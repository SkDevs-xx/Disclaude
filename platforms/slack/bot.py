"""
Slack × Claude Code Bot
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

import core.config as _cfg
from core.config import (
    BASE_DIR,
    load_config,
    load_platform_config,
    load_schedules,
    save_schedules,
    save_channel_name,
    get_channel_session,
    save_channel_session,
    get_model_config,
)
from core.engine import run_engine
from core.message import split_message
from core.skills import SkillRegistry
from platforms.base import PlatformContext
from platforms.slack import SLACK_FORMAT_HINT

if TYPE_CHECKING:
    from browser.manager import BrowserManager

logger = logging.getLogger("slack_bot")


class SlackBot:
    def __init__(self, bot_token: str, app_token: str):
        self.app = AsyncApp(token=bot_token)
        self.app_token = app_token
        self._bot_token = bot_token

        self.channel_locks: dict[str, asyncio.Lock] = {}
        self.running_tasks: dict[str, asyncio.Task] = {}
        self.running_processes: dict[str, asyncio.subprocess.Process] = {}
        self.scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
        self.browser_manager: BrowserManager | None = None

        self.platform_context = PlatformContext(
            name="slack",
            workspace_dir=_cfg.WORKFLOW_DIR,
            capabilities=frozenset({"block_kit", "slash_command", "thread"}),
            format_hint=SLACK_FORMAT_HINT,
        )
        self.skill_registry = SkillRegistry()
        self.skill_registry.scan_directory(BASE_DIR / "skills")

        self._register_handlers()

    def get_channel_lock(self, channel_id: str) -> asyncio.Lock:
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    def _register_handlers(self):
        """全 Cog を登録する。"""
        from platforms.slack.cogs import message, commands, heartbeat, review, schedule, summarize
        message.register(self)
        commands.register(self)
        heartbeat.register(self)
        review.register(self)
        schedule.register(self)
        summarize.register(self)

    def _reload_schedules(self):
        """schedules.json を読み込んでスケジューラに登録する。"""
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
                    replace_existing=True,
                    args=[s],
                    misfire_grace_time=60,
                )
            except Exception as e:
                logger.error("Schedule load error (%s): %s", s.get("id"), e)

    async def _run_schedule(self, s: dict, client=None):
        """スケジュールされたタスクを実行する。"""
        if client is None:
            client = self.app.client

        channel_id = s["channel_id"]

        try:
            if s.get("type") == "wrapup":
                from core.wrapup import run_wrapup
                from platforms.slack.utils import make_slack_collector
                cron_parts = s.get("cron", "0 5 * * *").split()
                sched_time = f"{int(cron_parts[1]):02d}:{int(cron_parts[0]):02d}"
                summary = await run_wrapup(
                    guild_id=0,
                    guild_name="Slack Workspace",
                    collect_messages=make_slack_collector(client),
                    format_hint=SLACK_FORMAT_HINT,
                    wrapup_time=sched_time,
                )
                if summary is None:
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=":information_source: 該当する会話履歴がありませんでした。",
                    )
                else:
                    for chunk in split_message(re.sub(r"\n{2,}", "\n", summary), max_len=3000):
                        await client.chat_postMessage(channel=channel_id, text=chunk)
            else:
                sched_model = s.get("model", "sonnet")
                sched_thinking = s.get("thinking", s.get("mode") == "planning")
                lock = self.get_channel_lock(channel_id)
                async with lock:
                    response, timed_out = await run_engine(
                        s["prompt"] + "\n\n" + SLACK_FORMAT_HINT,
                        model=sched_model,
                        thinking=sched_thinking,
                    )
                if timed_out:
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=":warning: スケジュールタスクがタイムアウトしました。",
                    )
                else:
                    for chunk in split_message(re.sub(r"\n{2,}", "\n", response), max_len=3000):
                        await client.chat_postMessage(channel=channel_id, text=chunk)
        except Exception as e:
            logger.exception("Schedule execution error (%s / %s): %s", s.get("id"), s.get("name"), e)
        finally:
            schedules = load_schedules()
            if schedules is not None:
                for item in schedules:
                    if item.get("id") == s.get("id") and item.get("id"):
                        item["run_count"] = item.get("run_count", 0) + 1
                        item["last_run"] = datetime.now(timezone.utc).isoformat()
                save_schedules(schedules)

    async def start(self):
        """Bot を起動する。"""
        # スケジュール読み込み
        self._reload_schedules()
        self.scheduler.start()
        logger.info("Scheduler started")

        # ブラウザ起動（設定されている場合）
        platform_cfg = load_platform_config()
        if platform_cfg.get("browser_enabled", False):
            from browser.manager import BrowserManager
            import os
            port = platform_cfg.get("browser_cdp_port", 9221)
            novnc_port = platform_cfg.get("browser_novnc_port", 6081)
            novnc_bind = load_config().get("novnc_bind_address", "localhost")
            profile_dir = os.path.expanduser("~/.config/clive-chrome-slack")
            vnc_port = platform_cfg.get("browser_vnc_port", 5901)
            vnc_display = platform_cfg.get("browser_vnc_display", ":100")
            self.browser_manager = BrowserManager(cdp_port=port, vnc_port=vnc_port, novnc_port=novnc_port, novnc_bind=novnc_bind, profile_dir=profile_dir, display=vnc_display)
            await self.browser_manager.start()

        # Socket Mode で接続
        self.handler = AsyncSocketModeHandler(self.app, self.app_token)
        logger.info("Starting Slack bot (Socket Mode)...")
        try:
            await self.handler.start_async()
        except asyncio.CancelledError:
            logger.info("Slack bot start task cancelled")
        finally:
            await self.stop()

    async def stop(self):
        """Bot を停止する。"""
        if hasattr(self, "handler") and self.handler:
            try:
                await self.handler.close_async()
            except Exception:
                pass
            self.handler = None
            
        if self.browser_manager:
            await self.browser_manager.stop()
        from core.attachments import close_http_session
        await close_http_session()
        self.scheduler.shutdown(wait=False)
