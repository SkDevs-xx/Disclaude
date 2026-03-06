"""
HeartbeatCog: 定期自律タスク（Heartbeat）管理
- X分ごとに HEARTBEAT.md を読み込み Claude で評価
- HEARTBEAT_OK → サイレント / WRAPUP_NEEDED → Wrap-up 実行 / その他 → 通知
- /heartbeat コマンドでステータス表示 + 設定変更 UI
"""

import hashlib
import logging
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import core.config as _cfg
from core.config import load_platform_config, save_platform_config, get_model_config
from core.engine import run_engine
from platforms.discord.utils import get_guild_channels
from platforms.discord.embeds import make_error_embed, make_info_embed
from core.message import split_message
from core.memory import (
    parse_heartbeat_state,
    update_heartbeat_state,
    get_checklist_section,
    update_checklist_section,
    should_run_wrapup,
)

JST = ZoneInfo("Asia/Tokyo")
logger = logging.getLogger("discord_bot")


def _heartbeat_file():
    """init_workspace() 後の WORKFLOW_DIR を反映して HEARTBEAT.md のパスを返す。"""
    return _cfg.WORKFLOW_DIR / "HEARTBEAT.md"


def _read_heartbeat_text() -> str:
    """HEARTBEAT.md の内容を返す。存在しなければ空文字列。"""
    hb = _heartbeat_file()
    return hb.read_text(encoding="utf-8") if hb.exists() else ""

# 重複抑制: {message_hash: last_sent_datetime}
_sent_warnings: dict[str, datetime] = {}
SUPPRESS_HOURS = 24


def _build_status_embed(state: dict, cfg: dict) -> discord.Embed:
    """ステータス表示用の Embed を組み立てる。"""
    enabled = cfg.get("heartbeat_enabled", True)
    thinking = cfg.get("heartbeat_thinking", False)
    ch_id = cfg.get("heartbeat_channel_id", "")
    interval = cfg.get("heartbeat_interval_minutes", 30)
    ch_name = f"<#{ch_id}>" if ch_id else "未設定"
    desc = (
        f"**Heartbeat:** {'ON' if enabled else 'OFF（Wrapup のみ）'}\n"
        f"**Thinking:** {'ON' if thinking else 'OFF'}\n"
        f"**通知チャンネル:** {ch_name}\n"
        f"**実行間隔:** {interval}分\n"
        f"**Wrapup時刻:** {state['wrapup_time']}\n"
        f"**Wrapup済み:** {'はい' if state['wrapup_done'] else 'いいえ'}\n"
        f"**最終更新:** {state.get('last_updated') or '未設定'}\n"
        f"**日次圧縮:** {state.get('last_wrapup_compressed') or '未実行'}\n"
        f"**週次圧縮:** {state.get('last_weekly_compressed') or '未実行'}"
    )
    return discord.Embed(title="Heartbeat", description=desc, color=discord.Color.green() if enabled else discord.Color.greyple())


# ─────────────────────────────────────────────
# 設定変更 Modal（詳細設定）
# ─────────────────────────────────────────────
class HeartbeatSettingsModal(discord.ui.Modal, title="Heartbeat 詳細設定"):
    def __init__(self, bot: commands.Bot, current_state: dict, current_cfg: dict):
        super().__init__()
        self.bot = bot

        self.wrapup_time_input = discord.ui.TextInput(
            label="Wrapup 時刻（HH:MM）",
            placeholder="05:00",
            default=current_state.get("wrapup_time", "05:00"),
            max_length=5,
            required=False,
        )
        self.interval_input = discord.ui.TextInput(
            label="実行間隔（分）",
            placeholder="30",
            default=str(current_cfg.get("heartbeat_interval_minutes", 30)),
            max_length=4,
            required=False,
        )
        hb_text = _read_heartbeat_text()
        self.checklist_input = discord.ui.TextInput(
            label="毎回チェック",
            style=discord.TextStyle.paragraph,
            default=get_checklist_section(hb_text),
            max_length=4000,
            required=False,
        )
        self.add_item(self.wrapup_time_input)
        self.add_item(self.interval_input)
        self.add_item(self.checklist_input)

    async def on_submit(self, interaction: discord.Interaction):
        errors = []

        # Wrapup 時刻
        new_time = self.wrapup_time_input.value.strip()
        if new_time:
            if not re.match(r"^\d{2}:\d{2}$", new_time):
                errors.append("Wrapup 時刻は HH:MM 形式で入力してください。")
            else:
                try:
                    h, m = new_time.split(":")
                    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                        raise ValueError
                    update_heartbeat_state(_heartbeat_file(),"wrapup_time", f'"{new_time}"')
                except ValueError:
                    errors.append("Wrapup 時刻が不正です。")

        # 実行間隔
        new_interval = self.interval_input.value.strip()
        if new_interval:
            try:
                minutes = int(new_interval)
                if minutes < 1:
                    raise ValueError
                cfg = load_platform_config()
                cfg["heartbeat_interval_minutes"] = minutes
                save_platform_config(cfg)
                self.bot.scheduler.add_job(
                    self.bot.get_cog("HeartbeatCog")._run_heartbeat,
                    IntervalTrigger(minutes=minutes),
                    id="heartbeat_main",
                    replace_existing=True,
                )
            except ValueError:
                errors.append("実行間隔は1以上の整数で入力してください。")

        # 毎回チェック
        new_checklist = self.checklist_input.value.strip()
        if new_checklist is not None:
            update_checklist_section(_heartbeat_file(),new_checklist)

        if errors:
            await interaction.response.send_message(
                embed=make_error_embed("\n".join(errors)), ephemeral=True
            )
            return

        # 更新後のステータスを表示
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        guild = interaction.guild
        channels = get_guild_channels(guild) if guild else []
        await interaction.response.edit_message(
            embed=_build_status_embed(state, cfg),
            view=HeartbeatView(self.bot, channels, cfg),
        )




# ─────────────────────────────────────────────
# 統合 View（チャンネルSelect + 今すぐ実行 + 詳細設定 + ON/OFF）
# ─────────────────────────────────────────────
class HeartbeatView(discord.ui.View):
    def __init__(self, bot: commands.Bot, channels: list[tuple[int, str]] | None = None, cfg: dict | None = None):
        super().__init__(timeout=120)
        self.bot = bot
        if cfg is None:
            cfg = load_platform_config()
        self._enabled = cfg.get("heartbeat_enabled", True)
        self._thinking = cfg.get("heartbeat_thinking", False)

        # チャンネル Select（row 0）
        if channels:
            current_ch = cfg.get("heartbeat_channel_id", "")
            options = [
                discord.SelectOption(
                    label=f"# {name}", value=str(cid),
                    default=(str(cid) == current_ch),
                )
                for cid, name in channels[:25]
            ]
            self.ch_select = discord.ui.Select(placeholder="通知チャンネルを選択", options=options, row=0)
            self.ch_select.callback = self._on_channel_select
            self.add_item(self.ch_select)

        self._update_toggle_buttons(self._enabled, self._thinking)

    def _update_toggle_buttons(self, enabled: bool, thinking: bool):
        self.hb_on_btn.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.hb_off_btn.style = discord.ButtonStyle.success if not enabled else discord.ButtonStyle.secondary
        self.thinking_on_btn.style = discord.ButtonStyle.success if thinking else discord.ButtonStyle.secondary
        self.thinking_off_btn.style = discord.ButtonStyle.success if not thinking else discord.ButtonStyle.secondary

    async def _on_channel_select(self, interaction: discord.Interaction):
        selected = interaction.data["values"][0]
        cfg = load_platform_config()
        cfg["heartbeat_channel_id"] = selected
        save_platform_config(cfg)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        for opt in self.ch_select.options:
            opt.default = (opt.value == selected)
        self._update_toggle_buttons(cfg.get("heartbeat_enabled", True), cfg.get("heartbeat_thinking", False))
        await interaction.response.edit_message(embed=_build_status_embed(state, cfg), view=self)

    @discord.ui.button(label="今すぐ実行", style=discord.ButtonStyle.primary, row=1)
    async def run_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cog: HeartbeatCog | None = self.bot.get_cog("HeartbeatCog")
        if cog:
            await cog._run_heartbeat()
        await interaction.delete_original_response()

    @discord.ui.button(label="詳細設定", style=discord.ButtonStyle.secondary, row=1)
    async def detail_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        await interaction.response.send_modal(HeartbeatSettingsModal(self.bot, state, cfg))

    @discord.ui.button(label="Heartbeat ON", row=2)
    async def hb_on_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._enabled = True
        cfg = load_platform_config()
        cfg["heartbeat_enabled"] = True
        save_platform_config(cfg)
        self._update_toggle_buttons(True, self._thinking)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        await interaction.response.edit_message(embed=_build_status_embed(state, cfg), view=self)

    @discord.ui.button(label="Heartbeat OFF", row=2)
    async def hb_off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._enabled = False
        cfg = load_platform_config()
        cfg["heartbeat_enabled"] = False
        save_platform_config(cfg)
        self._update_toggle_buttons(False, self._thinking)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        await interaction.response.edit_message(embed=_build_status_embed(state, cfg), view=self)

    @discord.ui.button(label="Thinking ON", row=3)
    async def thinking_on_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._thinking = True
        cfg = load_platform_config()
        cfg["heartbeat_thinking"] = True
        save_platform_config(cfg)
        self._update_toggle_buttons(self._enabled, True)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        await interaction.response.edit_message(embed=_build_status_embed(state, cfg), view=self)

    @discord.ui.button(label="Thinking OFF", row=3)
    async def thinking_off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._thinking = False
        cfg = load_platform_config()
        cfg["heartbeat_thinking"] = False
        save_platform_config(cfg)
        self._update_toggle_buttons(self._enabled, False)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        await interaction.response.edit_message(embed=_build_status_embed(state, cfg), view=self)


# ─────────────────────────────────────────────
# HeartbeatCog
# ─────────────────────────────────────────────
class HeartbeatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """APScheduler にジョブを登録する。"""
        cfg = load_platform_config()
        interval = cfg.get("heartbeat_interval_minutes", 30)
        self.bot.scheduler.add_job(
            self._run_heartbeat,
            IntervalTrigger(minutes=interval),
            id="heartbeat_main",
            replace_existing=True,
        )
        self.bot.scheduler.add_job(
            self._reset_wrapup_done,
            CronTrigger(hour=0, minute=0),
            id="heartbeat_midnight_reset",
            replace_existing=True,
        )
        logger.info("Heartbeat registered: interval=%dm", interval)

    # ── メイン処理 ──────────────────────────────

    async def _run_heartbeat(self) -> None:
        """Heartbeat メインループ。結果は通知チャンネルに送信する。"""
        text = _read_heartbeat_text()
        if not text.strip():
            return

        state = parse_heartbeat_state(text)

        # Python 側で wrapup 要否を事前判定
        wrapup_needed = should_run_wrapup(state)

        cfg = load_platform_config()
        notify_channel_id = cfg.get("heartbeat_channel_id")

        # Wrapup は ON/OFF 問わず常にチェック
        if wrapup_needed:
            await self._trigger_wrapup(notify_channel_id, state.get("wrapup_time", "05:00"))
            logger.info("Heartbeat: wrapup triggered")
            return

        # Heartbeat OFF → Claude 評価スキップ（Wrapup のみ動作）
        if not cfg.get("heartbeat_enabled", True):
            logger.info("Heartbeat: disabled, skipping Claude evaluation")
            return

        # Claude にチェックリストを評価させる
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        prompt = (
            f"現在時刻: {now_str}\n\n"
            + text
            + "\n\n---\n"
            "上記の HEARTBEAT チェックリストを確認してください。\n"
            "wrapup の実行が必要な場合は `WRAPUP_NEEDED` を含めてください。\n"
            "すべて問題なく報告事項がなければ `HEARTBEAT_OK` だけを返してください。\n"
            "報告事項がある場合は内容を日本語で送信してください（HEARTBEAT_OK は使わない）。"
        )

        # スキル instructions を注入
        ctx = self.bot.platform_context
        registry_instr = self.bot.skill_registry.build_instructions(
            ctx.name, disabled=ctx.disabled_skills,
        )
        skill_instr = (
            f"[platform: {ctx.name}]\n"
            + (f"\n{ctx.format_hint}\n" if ctx.format_hint else "")
            + (f"\n{registry_instr}" if registry_instr else "")
        )

        # タイムアウト = 実行間隔（次の heartbeat までに終わればよい）
        interval = cfg.get("heartbeat_interval_minutes", 30)
        hb_thinking = cfg.get("heartbeat_thinking", False)
        model, _ = get_model_config()
        response, timed_out = await run_engine(
            prompt, timeout=interval * 60, skill_instructions=skill_instr,
            model=model, thinking=hb_thinking,
        )

        if timed_out or not response:
            logger.warning("Heartbeat: Claude timed out or empty response")
            return

        # Claude が WRAPUP_NEEDED を返した場合（Python 事前判定は上で処理済み）
        if "WRAPUP_NEEDED" in response:
            await self._trigger_wrapup(notify_channel_id, state.get("wrapup_time", "05:00"))
            logger.info("Heartbeat: wrapup triggered by Claude")
            return

        # HEARTBEAT_OK キーワードを除去し、レポート部分があれば送信
        report = response.replace("HEARTBEAT_OK", "").strip()
        if report:
            logger.info("Heartbeat: OK (with report)")
            await self._notify(notify_channel_id, report)
        else:
            logger.info("Heartbeat: OK")
            await self._notify(notify_channel_id, "HEARTBEAT_OK", skip_dedup=True)

    # ── Wrapup トリガー ─────────────────────────

    async def _trigger_wrapup(self, notify_channel_id: str | None, wrapup_time: str = "05:00"):
        """全ギルドに対して Wrap-up を実行し、結果を通知チャンネルに送信する。"""
        from core.wrapup import run_wrapup
        from platforms.discord.utils import make_discord_collector
        from platforms.discord import DISCORD_FORMAT_HINT

        any_success = False
        for guild in self.bot.guilds:
            try:
                summary = await run_wrapup(
                    guild_id=guild.id,
                    guild_name=guild.name,
                    collect_messages=make_discord_collector(guild),
                    format_hint=DISCORD_FORMAT_HINT,
                    wrapup_time=wrapup_time,
                )
                if summary:
                    any_success = True
                    if notify_channel_id:
                        channel = self.bot.get_channel(int(notify_channel_id))
                        if channel:
                            header = f"**Wrap-up ({guild.name})**\n"
                            for chunk in split_message(header + summary, max_len=2000):
                                await channel.send(content=chunk)
            except Exception as e:
                logger.exception("Heartbeat: wrapup error for guild %s: %s", guild.name, e)

        if any_success:
            update_heartbeat_state(_heartbeat_file(),"wrapup_done", "true")
            update_heartbeat_state(_heartbeat_file(),"last_updated", datetime.now(JST).strftime("%Y-%m-%d"))
            logger.info("Heartbeat: wrapup completed, wrapup_done=true")
        else:
            logger.warning("Heartbeat: wrapup failed for all guilds, wrapup_done remains false")

        # 圧縮チェック
        state = parse_heartbeat_state(_read_heartbeat_text())
        for guild in self.bot.guilds:
            await self._maybe_compress(guild.id, state)

    # ── 日次リセット ────────────────────────────

    async def _reset_wrapup_done(self):
        """毎日0:00に wrapup_done を false にリセットする。"""
        update_heartbeat_state(_heartbeat_file(),"wrapup_done", "false")
        update_heartbeat_state(_heartbeat_file(),"last_updated", datetime.now(JST).strftime("%Y-%m-%d"))
        logger.info("Heartbeat: midnight reset, wrapup_done=false")

    # ── 圧縮ロジック ────────────────────────────

    async def _maybe_compress(self, guild_id: int, state: dict):
        """日次→週次、週次→月次の圧縮が必要かチェックし、必要なら実行する。"""
        from core.wrapup import get_wrapup_dir

        today = datetime.now(JST).date()
        guild_dir = get_wrapup_dir() / str(guild_id)
        if not guild_dir.exists():
            return

        # 日次 → 週次（7日経過）
        last_compressed = state.get("last_wrapup_compressed")
        if not last_compressed:
            update_heartbeat_state(_heartbeat_file(),"last_wrapup_compressed", str(today))
        else:
            try:
                last = date.fromisoformat(last_compressed)
                if (today - last).days >= 7:
                    await self._compress_daily_to_weekly(guild_id, guild_dir, today)
                    update_heartbeat_state(_heartbeat_file(),"last_wrapup_compressed", str(today))
            except ValueError:
                update_heartbeat_state(_heartbeat_file(),"last_wrapup_compressed", str(today))

        # 週次 → 月次（28日経過）
        last_weekly = state.get("last_weekly_compressed")
        if not last_weekly:
            update_heartbeat_state(_heartbeat_file(),"last_weekly_compressed", str(today))
        else:
            try:
                last = date.fromisoformat(last_weekly)
                if (today - last).days >= 28:
                    await self._compress_weekly_to_monthly(guild_id, guild_dir, today)
                    update_heartbeat_state(_heartbeat_file(),"last_weekly_compressed", str(today))
            except ValueError:
                update_heartbeat_state(_heartbeat_file(),"last_weekly_compressed", str(today))

    async def _compress_daily_to_weekly(self, guild_id: int, guild_dir, today: date):
        """7日以上前の日次ファイルを週次サマリーに圧縮する。"""
        old_files = []
        for f in guild_dir.glob("????-??-??.md"):
            try:
                file_date = date.fromisoformat(f.stem)
                if (today - file_date).days >= 7:
                    old_files.append(f)
            except ValueError:
                continue

        if not old_files:
            return

        old_files.sort(key=lambda f: f.stem)
        contents = []
        for f in old_files:
            contents.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')}")

        combined = "\n\n---\n\n".join(contents)
        iso_cal = today.isocalendar()
        week_file = guild_dir / f"{iso_cal.year}-W{iso_cal.week:02d}.md"

        prompt = (
            "以下は過去の日次Wrap-upサマリーです。これらを1つの週次サマリーに圧縮してください。\n"
            "重要なポイント・決定事項・進捗を残し、冗長な詳細は省いてください。\n\n"
            + combined
        )

        summary, timed_out = await run_engine(prompt)
        if timed_out or not summary:
            logger.warning("Heartbeat: weekly compression timed out")
            return

        week_file.write_text(f"# 週次サマリー ({iso_cal.year}-W{iso_cal.week:02d})\n\n{summary}\n", encoding="utf-8")

        for f in old_files:
            f.unlink()

        logger.info("Heartbeat: compressed %d daily files -> %s", len(old_files), week_file.name)

    async def _compress_weekly_to_monthly(self, guild_id: int, guild_dir, today: date):
        """4週以上前の週次ファイルを月次サマリーに圧縮する。"""
        old_files = []
        for f in guild_dir.glob("????-W??.md"):
            try:
                year, week = f.stem.split("-W")
                file_date = date.fromisocalendar(int(year), int(week), 1)
                if (today - file_date).days >= 28:
                    old_files.append(f)
            except (ValueError, IndexError):
                continue

        if not old_files:
            return

        old_files.sort(key=lambda f: f.stem)
        contents = []
        for f in old_files:
            contents.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')}")

        combined = "\n\n---\n\n".join(contents)
        month_file = guild_dir / f"{today.year}-{today.month:02d}.md"

        prompt = (
            "以下は過去の週次Wrap-upサマリーです。これらを1つの月次サマリーに圧縮してください。\n"
            "重要なトレンド・決定事項・マイルストーンを残し、詳細は省いてください。\n\n"
            + combined
        )

        summary, timed_out = await run_engine(prompt)
        if timed_out or not summary:
            logger.warning("Heartbeat: monthly compression timed out")
            return

        month_file.write_text(f"# 月次サマリー ({today.year}-{today.month:02d})\n\n{summary}\n", encoding="utf-8")

        for f in old_files:
            f.unlink()

        logger.info("Heartbeat: compressed %d weekly files -> %s", len(old_files), month_file.name)

    # ── 通知（重複抑制） ────────────────────────

    async def _notify(self, channel_id_str: str | None, message: str, *, skip_dedup: bool = False):
        """通知チャンネルへ送信する。24h 以内の同一メッセージは抑制する。"""
        if not channel_id_str:
            logger.warning("Heartbeat: no notification channel configured")
            return

        now = datetime.now(JST)

        msg_hash: str | None = None
        if not skip_dedup:
            # 期限切れエントリを削除
            cutoff = timedelta(hours=SUPPRESS_HOURS)
            expired = [k for k, v in _sent_warnings.items() if (now - v) >= cutoff]
            for k in expired:
                del _sent_warnings[k]

            msg_hash = hashlib.md5(message[:200].encode()).hexdigest()
            if msg_hash in _sent_warnings:
                age = now - _sent_warnings[msg_hash]
                if age.total_seconds() < SUPPRESS_HOURS * 3600:
                    logger.info("Heartbeat: suppressed duplicate warning")
                    return

        channel = self.bot.get_channel(int(channel_id_str))
        if not channel:
            logger.warning("Heartbeat: notification channel not found: %s", channel_id_str)
            return

        if msg_hash is not None:
            _sent_warnings[msg_hash] = now
        for chunk in split_message(message, max_len=2000):
            await channel.send(content=chunk)

    # ─────────────────────────────────────────────
    # /heartbeat コマンド（単体）
    # ─────────────────────────────────────────────
    @app_commands.command(name="heartbeat", description="Heartbeatの状態表示・設定変更")
    async def heartbeat_command(self, interaction: discord.Interaction):
        text = _read_heartbeat_text()
        if not text:
            await interaction.response.send_message(
                embed=make_error_embed("HEARTBEAT.md が見つかりません。"), ephemeral=True
            )
            return
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        channels = get_guild_channels(interaction.guild) if interaction.guild else []
        await interaction.response.send_message(
            embed=_build_status_embed(state, cfg),
            view=HeartbeatView(self.bot, channels, cfg),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(HeartbeatCog(bot))
