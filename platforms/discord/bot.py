"""
Discord × Claude Code Bot
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import core.config as _cfg
from core.config import (
    BASE_DIR, load_config, load_platform_config,
    load_schedules, save_schedules, save_channel_name,
    get_channel_session, save_channel_session,
    get_no_mention_channels, get_model_config,
)
from core.engine import run_engine
from core.message import split_message
from core.attachments import process_attachment
from core.skills import SkillRegistry
from platforms.base import PlatformContext
from platforms.discord import DISCORD_FORMAT_HINT
from platforms.discord.embeds import make_error_embed, make_info_embed

if TYPE_CHECKING:
    from browser.manager import BrowserManager

logger = logging.getLogger("discord_bot")


# ─────────────────────────────────────────────
# ボット本体
# ─────────────────────────────────────────────
class ClaudeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.channel_locks: dict[int, asyncio.Lock] = {}
        self.running_tasks: dict[int, asyncio.Task] = {}  # channel_id -> Task
        self.running_processes: dict[int, asyncio.subprocess.Process] = {}  # channel_id -> Claude Process
        self.scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
        self.browser_manager: BrowserManager | None = None
        self.platform_context = PlatformContext(
            name="discord",
            workspace_dir=_cfg.WORKFLOW_DIR,
            capabilities=frozenset({"embed", "reaction", "thread", "slash_command"}),
            format_hint=DISCORD_FORMAT_HINT,
        )
        self.skill_registry = SkillRegistry()
        self.skill_registry.scan_directory(BASE_DIR / "skills")

    def get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    async def setup_hook(self):
        for ext in [
            "platforms.discord.cogs.utility",
            "platforms.discord.cogs.schedule",
            "platforms.discord.cogs.summarize",
            "platforms.discord.cogs.heartbeat",
            "platforms.discord.cogs.review",
        ]:
            await self.load_extension(ext)
        await self.tree.sync()
        logger.info("Slash commands synced")

        self._reload_schedules()
        self.scheduler.start()

        platform_cfg = load_platform_config()
        if platform_cfg.get("browser_enabled", False):
            from browser.manager import BrowserManager
            import os
            port = platform_cfg.get("browser_cdp_port", 9222)
            novnc_port = platform_cfg.get("browser_novnc_port", 6080)
            vnc_port = platform_cfg.get("browser_vnc_port", 5900)
            vnc_display = platform_cfg.get("browser_vnc_display", ":99")
            novnc_bind = load_config().get("novnc_bind_address", "localhost")
            profile_dir = os.path.expanduser("~/.config/clive-chrome-discord")
            self.browser_manager = BrowserManager(cdp_port=port, vnc_port=vnc_port, novnc_port=novnc_port, novnc_bind=novnc_bind, profile_dir=profile_dir, display=vnc_display)
            await self.browser_manager.start()

    def _reload_schedules(self):
        """schedules.json を読み込んでスケジューラに登録"""
        schedules = load_schedules()
        if schedules is None:
            logger.error("schedules.json is corrupted. Skipping schedule reload.")
            return

        for job in self.scheduler.get_jobs():
            if job.id.startswith("sched_"):
                job.remove()

        for s in schedules:
            if s.get("status") != "active":
                continue
            try:
                trigger = CronTrigger.from_crontab(s["cron"])
                self.scheduler.add_job(
                    self._run_schedule,
                    trigger,
                    id=f"sched_{s['id']}",
                    args=[s],
                    replace_existing=True,
                    misfire_grace_time=60,
                )
            except Exception as e:
                logger.error("Schedule load error (%s): %s", s.get("id"), e)

    async def _run_schedule(self, s: dict):
        channel_id = int(s["channel_id"])
        channel = self.get_channel(channel_id)
        if not channel:
            logger.warning("Schedule channel not found: %d", channel_id)
            return

        try:
            if s.get("type") == "wrapup":
                from core.wrapup import run_wrapup
                from platforms.discord.utils import make_discord_collector
                # cron から時刻を取得（例: "0 5 * * *" → "05:00"）
                cron_parts = s.get("cron", "0 5 * * *").split()
                sched_time = f"{int(cron_parts[1]):02d}:{int(cron_parts[0]):02d}"
                guild = channel.guild if hasattr(channel, "guild") else None
                if guild is None:
                    logger.warning("Schedule wrapup: guild not found for channel %d", channel_id)
                    return
                summary = await run_wrapup(
                    guild_id=guild.id,
                    guild_name=guild.name,
                    collect_messages=make_discord_collector(guild),
                    format_hint=DISCORD_FORMAT_HINT,
                    wrapup_time=sched_time,
                )
                if summary is None:
                    await channel.send(embed=make_info_embed("ラップアップ", "該当する会話履歴がありませんでした。"))
                else:
                    for chunk in split_message(re.sub(r'\n{2,}', '\n', summary), max_len=2000):
                        await channel.send(content=chunk)
            else:
                # 後方互換: 旧 "mode" キーを model+thinking に変換
                sched_model = s.get("model", "sonnet")
                sched_thinking = s.get("thinking", s.get("mode") == "planning")
                async with self.get_channel_lock(channel_id):
                    response, timed_out = await run_engine(
                        s["prompt"] + "\n\n" + DISCORD_FORMAT_HINT,
                        model=sched_model, thinking=sched_thinking,
                    )

                if timed_out:
                    await channel.send(embed=make_error_embed("スケジュールタスクがタイムアウトしました。"))
                else:
                    for chunk in split_message(re.sub(r'\n{2,}', '\n', response), max_len=2000):
                        await channel.send(content=chunk)
        except Exception as e:
            logger.exception("Schedule execution error (%s / %s): %s", s.get("id"), s.get("name"), e)
        finally:
            # run_count / last_run は成功・失敗に関わらず更新
            schedules = load_schedules()
            if schedules is not None:
                for item in schedules:
                    if item["id"] == s["id"]:
                        item["run_count"] = item.get("run_count", 0) + 1
                        item["last_run"] = datetime.now(timezone.utc).isoformat()
                save_schedules(schedules)

    async def close(self):
        if self.browser_manager:
            await self.browser_manager.stop()
        from core.attachments import close_http_session
        await close_http_session()
        await super().close()

    async def on_ready(self):
        logger.info("Bot ready: %s (ID: %s)", self.user, self.user.id)
        for guild in self.guilds:
            for channel in guild.channels:
                if isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
                    save_channel_name(channel.id, channel.name)
            # ギルドコマンドの重複をクリア（グローバルのみに統一）
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Cleared guild commands for: %s", guild.name)
        logger.info("Channel names cached: %d channels", sum(len(g.channels) for g in self.guilds))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """全スラッシュコマンドに allowed_user_ids チェックを適用する。"""
        allowed = load_platform_config().get("allowed_user_ids", [])
        if str(interaction.user.id) not in allowed:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return False
        return True

    async def on_message(self, message: discord.Message):
        logger.info(
            "[on_message] author=%s (id=%s, bot=%s) channel=%s(%s) content_len=%d",
            message.author,
            message.author.id,
            message.author.bot,
            message.channel,
            type(message.channel).__name__,
            len(message.content or ""),
        )

        if message.author.bot:
            return

        await self.process_commands(message)

        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            logger.info("[on_message] skipped: channel type %s", type(message.channel).__name__)
            return

        platform_cfg = load_platform_config()
        no_mention = get_no_mention_channels()
        if self.user not in message.mentions and str(message.channel.id) not in no_mention:
            return

        allowed = platform_cfg.get("allowed_user_ids", [])
        if str(message.author.id) not in allowed:
            logger.info("[on_message] skipped: user %s not in allowlist %s", message.author.id, allowed)
            return

        channel_id = message.channel.id
        save_channel_name(channel_id, message.channel.name)

        content_raw = message.content or ""
        if self.user:
            content_raw = re.sub(rf"<@!?{self.user.id}>", "", content_raw)
        user_content = content_raw.strip()

        if not user_content and not message.attachments:
            logger.warning(
                "Empty message from %s in ch %d — "
                "Discord Developer Portal で 'Message Content Intent' が有効か確認してください。",
                message.author,
                channel_id,
            )
            return

        try:
            injected_text = ""
            image_paths: list[Path] = []
            for att in message.attachments:
                text_part, image_path = await process_attachment(att)
                if text_part:
                    injected_text += text_part
                if image_path is not None:
                    image_paths.append(image_path)

            if not user_content and not injected_text:
                return

            full_prompt = ""
            if user_content:
                full_prompt += f"<user_input>\n{user_content}\n</user_input>\n"
            if injected_text:
                full_prompt += f"<attachments>\n{injected_text}\n</attachments>\n"
            full_prompt = full_prompt.strip()

            try:
                await message.add_reaction("🤔")
            except Exception:
                pass
            async with message.channel.typing():
                async with self.get_channel_lock(channel_id):
                    session_id = get_channel_session(channel_id)
                    is_new = session_id is None
                    if is_new:
                        session_id = str(uuid.uuid4())
                        save_channel_session(channel_id, session_id)

                    task = asyncio.current_task()
                    self.running_tasks[channel_id] = task

                    model, thinking = get_model_config()
                    registry_instr = self.skill_registry.build_instructions(
                        self.platform_context.name,
                        disabled=self.platform_context.disabled_skills,
                    )
                    skill_instr = (
                        f"[platform: {self.platform_context.name}]\n"
                        + (f"\n{registry_instr}" if registry_instr else "")
                    )
                    response, timed_out = await run_engine(
                        full_prompt, model=model, thinking=thinking,
                        session_id=session_id,
                        is_new_session=is_new,
                        on_process=lambda p: self.running_processes.__setitem__(channel_id, p),
                        skill_instructions=skill_instr,
                    )

            if timed_out:
                await message.reply(embed=make_error_embed(
                    "タイムアウトしました。`/cancel` で再試行するか、少し待ってから再送してください。"
                ))
                return

            # Discord 表示用: 連続改行を単一改行に正規化
            display_response = re.sub(r'\n{2,}', '\n', response)
            chunks = split_message(display_response, max_len=2000)
            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        finally:
            self.running_tasks.pop(channel_id, None)
            self.running_processes.pop(channel_id, None)
            try:
                await message.remove_reaction("🤔", self.user)
            except Exception:
                pass
            # workspace/temp/ の画像ファイルをクリーンアップ
            for p in image_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    logger.warning("画像クリーンアップ失敗: %s", p)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.exception("App command error: %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(f"コマンドエラー: {error}"), ephemeral=True
            )
