"""
Slack Heartbeat ハンドラ
- 定期自律タスク（HEARTBEAT.md チェックリスト評価）
- /heartbeat コマンドでステータス表示・設定変更
Discord の HeartbeatCog と同等のロジック
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, date, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import core.config as _cfg
from core.config import load_platform_config, save_platform_config, get_model_config
from core.claude import run_claude
from core.message import split_message
from core.memory import (
    parse_heartbeat_state,
    update_heartbeat_state,
    get_checklist_section,
    update_checklist_section,
    should_run_wrapup,
)

if TYPE_CHECKING:
    from platforms.slack.bot import SlackBot

JST = ZoneInfo("Asia/Tokyo")
logger = logging.getLogger("slack_bot")

_sent_warnings: dict[str, datetime] = {}
SUPPRESS_HOURS = 24


def _heartbeat_file():
    return _cfg.WORKFLOW_DIR / "HEARTBEAT.md"


def _read_heartbeat_text() -> str:
    hb = _heartbeat_file()
    return hb.read_text(encoding="utf-8") if hb.exists() else ""


def _status_text(state: dict, cfg: dict) -> str:
    enabled = cfg.get("heartbeat_enabled", True)
    ch_id = cfg.get("heartbeat_channel_id", "")
    interval = cfg.get("heartbeat_interval_minutes", 30)
    ch_ref = f"<#{ch_id}>" if ch_id else "未設定"
    return (
        f"*Heartbeat ステータス*\n"
        f"- Heartbeat: *{'ON' if enabled else 'OFF（Wrapup のみ）'}*\n"
        f"- 通知チャンネル: {ch_ref}\n"
        f"- 実行間隔: *{interval}分*\n"
        f"- Wrapup時刻: *{state['wrapup_time']}*\n"
        f"- Wrapup済み: *{'はい' if state['wrapup_done'] else 'いいえ'}*\n"
        f"- 最終更新: {state.get('last_updated') or '未設定'}\n"
        f"- 日次圧縮: {state.get('last_wrapup_compressed') or '未実行'}\n"
        f"- 週次圧縮: {state.get('last_weekly_compressed') or '未実行'}"
    )


def _thinking_blocks(thinking: bool) -> list[dict]:
    on_btn: dict = {"type": "button", "action_id": "heartbeat_thinking_on", "text": {"type": "plain_text", "text": "Thinking ON"}}
    off_btn: dict = {"type": "button", "action_id": "heartbeat_thinking_off", "text": {"type": "plain_text", "text": "Thinking OFF"}}
    if thinking:
        on_btn["style"] = "primary"
    else:
        off_btn["style"] = "primary"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Heartbeat Thinking*: *{'ON' if thinking else 'OFF'}*"}},
        {"type": "actions", "block_id": "hb_thinking_block", "elements": [on_btn, off_btn]},
    ]


def _status_blocks(state: dict, cfg: dict, channels: list[tuple[str, str]]) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _status_text(state, cfg)}},
        {"type": "divider"},
    ]

    # チャンネル選択
    if channels:
        current_ch = cfg.get("heartbeat_channel_id", "")
        options = [
            {
                "text": {"type": "plain_text", "text": f"# {name}"},
                "value": ch_id,
            }
            for ch_id, name in channels[:25]
        ]
        initial = next((o for o in options if o["value"] == current_ch), options[0]) if options else None
        select_element: dict = {
            "type": "static_select",
            "action_id": "heartbeat_channel_select",
            "placeholder": {"type": "plain_text", "text": "通知チャンネルを選択"},
            "options": options,
        }
        if initial:
            select_element["initial_option"] = initial
        blocks.append({"type": "actions", "block_id": "hb_channel_block", "elements": [select_element]})

    # ボタン群
    enabled = cfg.get("heartbeat_enabled", True)
    toggle_on: dict = {"type": "button", "action_id": "heartbeat_toggle_on", "text": {"type": "plain_text", "text": "Heartbeat ON"}}
    toggle_off: dict = {"type": "button", "action_id": "heartbeat_toggle_off", "text": {"type": "plain_text", "text": "Heartbeat OFF"}}
    if enabled:
        toggle_on["style"] = "primary"
    else:
        toggle_off["style"] = "danger"
    blocks.append({
        "type": "actions",
        "block_id": "hb_control_block",
        "elements": [
            {"type": "button", "action_id": "heartbeat_run_now", "text": {"type": "plain_text", "text": "今すぐ実行"}, "style": "primary"},
            {"type": "button", "action_id": "heartbeat_settings", "text": {"type": "plain_text", "text": "詳細設定"}},
            toggle_on,
            toggle_off,
        ],
    })

    hb_thinking = cfg.get("heartbeat_thinking", False)
    blocks.extend(_thinking_blocks(hb_thinking))

    return blocks


async def _run_heartbeat_core(bot: "SlackBot"):
    """Heartbeat メインループ。"""
    text = _read_heartbeat_text()
    if not text.strip():
        return

    state = parse_heartbeat_state(text)
    wrapup_needed = should_run_wrapup(state)
    cfg = load_platform_config()
    notify_channel_id = cfg.get("heartbeat_channel_id")

    if wrapup_needed:
        await _trigger_wrapup(bot, notify_channel_id, state.get("wrapup_time", "05:00"))
        logger.info("Heartbeat: wrapup triggered")
        return

    if not cfg.get("heartbeat_enabled", True):
        logger.info("Heartbeat: disabled, skipping Claude evaluation")
        return

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

    ctx = bot.platform_context
    registry_instr = bot.skill_registry.build_instructions(ctx.name, disabled=ctx.disabled_skills)
    skill_instr = (
        f"[platform: {ctx.name}]\n"
        + (f"\n{ctx.format_hint}\n" if ctx.format_hint else "")
        + (f"\n{registry_instr}" if registry_instr else "")
    )

    interval = cfg.get("heartbeat_interval_minutes", 30)
    model, _ = get_model_config()
    hb_thinking = cfg.get("heartbeat_thinking", False)
    response, timed_out = await run_claude(
        prompt, timeout=interval * 60, skill_instructions=skill_instr, model=model, thinking=hb_thinking,
    )

    if timed_out or not response:
        logger.warning("Heartbeat: Claude timed out or empty response")
        return

    if "WRAPUP_NEEDED" in response:
        await _trigger_wrapup(bot, notify_channel_id, state.get("wrapup_time", "05:00"))
        logger.info("Heartbeat: wrapup triggered by Claude")
        return

    report = response.replace("HEARTBEAT_OK", "").strip()
    if report:
        logger.info("Heartbeat: OK (with report)")
        await _notify(bot, notify_channel_id, report)
    else:
        logger.info("Heartbeat: OK")
        await _notify(bot, notify_channel_id, "HEARTBEAT_OK", skip_dedup=True)


async def _trigger_wrapup(bot: "SlackBot", notify_channel_id: str | None, wrapup_time: str = "05:00"):
    from core.wrapup import run_wrapup
    from platforms.slack import SLACK_FORMAT_HINT
    from platforms.slack.utils import make_slack_collector

    client = bot.app.client
    try:
        summary = await run_wrapup(
            guild_id=0,
            guild_name="Slack Workspace",
            collect_messages=make_slack_collector(client),
            format_hint=SLACK_FORMAT_HINT,
            wrapup_time=wrapup_time,
        )
        if summary and notify_channel_id:
            for chunk in split_message(summary, max_len=3000):
                await client.chat_postMessage(channel=notify_channel_id, text=chunk)
        update_heartbeat_state(_heartbeat_file(), "wrapup_done", "true")
        update_heartbeat_state(_heartbeat_file(), "last_updated", datetime.now(JST).strftime("%Y-%m-%d"))
        logger.info("Heartbeat: wrapup completed")
    except Exception as e:
        logger.exception("Heartbeat: wrapup error: %s", e)

    state = parse_heartbeat_state(_read_heartbeat_text())
    await _maybe_compress(bot, state)


async def _maybe_compress(bot: "SlackBot", state: dict):
    from core.wrapup import get_wrapup_dir

    today = datetime.now(JST).date()
    guild_dir = get_wrapup_dir() / "0"
    if not guild_dir.exists():
        return

    last_compressed = state.get("last_wrapup_compressed")
    if not last_compressed:
        update_heartbeat_state(_heartbeat_file(), "last_wrapup_compressed", str(today))
    else:
        try:
            last = date.fromisoformat(last_compressed)
            if (today - last).days >= 7:
                await _compress_daily_to_weekly(guild_dir, today)
                update_heartbeat_state(_heartbeat_file(), "last_wrapup_compressed", str(today))
        except ValueError:
            update_heartbeat_state(_heartbeat_file(), "last_wrapup_compressed", str(today))

    last_weekly = state.get("last_weekly_compressed")
    if not last_weekly:
        update_heartbeat_state(_heartbeat_file(), "last_weekly_compressed", str(today))
    else:
        try:
            last = date.fromisoformat(last_weekly)
            if (today - last).days >= 28:
                await _compress_weekly_to_monthly(guild_dir, today)
                update_heartbeat_state(_heartbeat_file(), "last_weekly_compressed", str(today))
        except ValueError:
            update_heartbeat_state(_heartbeat_file(), "last_weekly_compressed", str(today))


async def _compress_daily_to_weekly(guild_dir, today: date):
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
    contents = [f"## {f.stem}\n{f.read_text(encoding='utf-8')}" for f in old_files]
    combined = "\n\n---\n\n".join(contents)
    iso_cal = today.isocalendar()
    week_file = guild_dir / f"{iso_cal.year}-W{iso_cal.week:02d}.md"
    prompt = (
        "以下は過去の日次Wrap-upサマリーです。これらを1つの週次サマリーに圧縮してください。\n"
        "重要なポイント・決定事項・進捗を残し、冗長な詳細は省いてください。\n\n"
        + combined
    )
    summary, timed_out = await run_claude(prompt)
    if timed_out or not summary:
        return
    week_file.write_text(f"# 週次サマリー ({iso_cal.year}-W{iso_cal.week:02d})\n\n{summary}\n", encoding="utf-8")
    for f in old_files:
        f.unlink()
    logger.info("Heartbeat: compressed %d daily files -> %s", len(old_files), week_file.name)


async def _compress_weekly_to_monthly(guild_dir, today: date):
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
    contents = [f"## {f.stem}\n{f.read_text(encoding='utf-8')}" for f in old_files]
    combined = "\n\n---\n\n".join(contents)
    month_file = guild_dir / f"{today.year}-{today.month:02d}.md"
    prompt = (
        "以下は過去の週次Wrap-upサマリーです。これらを1つの月次サマリーに圧縮してください。\n"
        "重要なトレンド・決定事項・マイルストーンを残し、詳細は省いてください。\n\n"
        + combined
    )
    summary, timed_out = await run_claude(prompt)
    if timed_out or not summary:
        return
    month_file.write_text(f"# 月次サマリー ({today.year}-{today.month:02d})\n\n{summary}\n", encoding="utf-8")
    for f in old_files:
        f.unlink()
    logger.info("Heartbeat: compressed %d weekly files -> %s", len(old_files), month_file.name)


async def _notify(bot: "SlackBot", channel_id_str: str | None, message: str, *, skip_dedup: bool = False):
    if not channel_id_str:
        logger.warning("Heartbeat: no notification channel configured")
        return

    now = datetime.now(JST)

    if not skip_dedup:
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

    if not skip_dedup:
        msg_hash = hashlib.md5(message[:200].encode()).hexdigest()
        _sent_warnings[msg_hash] = now

    client = bot.app.client
    for chunk in split_message(message, max_len=3000):
        await client.chat_postMessage(channel=channel_id_str, text=chunk)


def register(bot: "SlackBot"):
    """Heartbeat スケジューラ・コマンドを登録する。"""
    app = bot.app

    # APScheduler にジョブを登録
    cfg = load_platform_config()
    interval = cfg.get("heartbeat_interval_minutes", 30)
    bot.scheduler.add_job(
        _run_heartbeat_core,
        IntervalTrigger(minutes=interval),
        id="heartbeat_main",
        replace_existing=True,
        args=[bot],
    )
    bot.scheduler.add_job(
        _reset_wrapup_done,
        CronTrigger(hour=0, minute=0),
        id="heartbeat_midnight_reset",
        replace_existing=True,
        args=[],
    )
    logger.info("Heartbeat registered: interval=%dm", interval)

    # ── /heartbeat コマンド ──────────────────────────────
    @app.command("/heartbeat-ai")
    async def cmd_heartbeat(ack, command, client):
        await ack()
        channel_id = command["channel_id"]
        user_id = command["user_id"]
        text = _read_heartbeat_text()
        if not text:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=":warning: HEARTBEAT.md が見つかりません。",
            )
            return
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        from platforms.slack.utils import get_workspace_channels
        channels = await get_workspace_channels(client)
        await client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            blocks=_status_blocks(state, cfg, channels),
            text="Heartbeat ステータス",
        )

    @app.action("heartbeat_channel_select")
    async def action_heartbeat_channel_select(ack, body, respond, client):
        await ack()
        selected = body["actions"][0]["selected_option"]["value"]
        cfg = load_platform_config()
        cfg["heartbeat_channel_id"] = selected
        save_platform_config(cfg)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        from platforms.slack.utils import get_workspace_channels
        channels = await get_workspace_channels(client)
        await respond(blocks=_status_blocks(state, cfg, channels), text="Heartbeat ステータス", replace_original=True)

    @app.action("heartbeat_run_now")
    async def action_heartbeat_run_now(ack, respond):
        await ack()
        await respond(text=":hourglass: Heartbeat を実行中...", replace_original=False)
        await _run_heartbeat_core(bot)

    @app.action("heartbeat_settings")
    async def action_heartbeat_settings(ack, body, client):
        await ack()
        trigger_id = body["trigger_id"]
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        await client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "heartbeat_settings_modal",
                "title": {"type": "plain_text", "text": "Heartbeat 詳細設定"},
                "submit": {"type": "plain_text", "text": "保存"},
                "close": {"type": "plain_text", "text": "キャンセル"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "wrapup_time_block",
                        "label": {"type": "plain_text", "text": "Wrapup 時刻（HH:MM）"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "wrapup_time_input",
                            "initial_value": state.get("wrapup_time", "05:00"),
                            "placeholder": {"type": "plain_text", "text": "05:00"},
                        },
                        "optional": True,
                    },
                    {
                        "type": "input",
                        "block_id": "interval_block",
                        "label": {"type": "plain_text", "text": "実行間隔（分）"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "interval_input",
                            "initial_value": str(cfg.get("heartbeat_interval_minutes", 30)),
                            "placeholder": {"type": "plain_text", "text": "30"},
                        },
                        "optional": True,
                    },
                    {
                        "type": "input",
                        "block_id": "checklist_block",
                        "label": {"type": "plain_text", "text": "毎回チェック"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "checklist_input",
                            "multiline": True,
                            "initial_value": get_checklist_section(text),
                            "placeholder": {"type": "plain_text", "text": "チェックリストを入力..."},
                        },
                        "optional": True,
                    },
                ],
            },
        )

    @app.view("heartbeat_settings_modal")
    async def handle_heartbeat_settings_modal(ack, body, view, client):
        errors: dict[str, str] = {}
        values = view["state"]["values"]

        new_time = values.get("wrapup_time_block", {}).get("wrapup_time_input", {}).get("value", "").strip()
        if new_time:
            import re as _re
            if not _re.match(r"^\d{2}:\d{2}$", new_time):
                errors["wrapup_time_block"] = "HH:MM 形式で入力してください。"
            else:
                h, m = new_time.split(":")
                if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                    errors["wrapup_time_block"] = "有効な時刻を入力してください。"

        new_interval = values.get("interval_block", {}).get("interval_input", {}).get("value", "").strip()
        if new_interval:
            try:
                minutes = int(new_interval)
                if minutes < 1:
                    raise ValueError
            except ValueError:
                errors["interval_block"] = "1以上の整数で入力してください。"

        if errors:
            await ack(response_action="errors", errors=errors)
            return

        await ack()

        if new_time:
            update_heartbeat_state(_heartbeat_file(), "wrapup_time", f'"{new_time}"')

        if new_interval:
            minutes = int(new_interval)
            cfg = load_platform_config()
            cfg["heartbeat_interval_minutes"] = minutes
            save_platform_config(cfg)
            bot.scheduler.add_job(
                _run_heartbeat_core,
                IntervalTrigger(minutes=minutes),
                id="heartbeat_main",
                replace_existing=True,
                args=[bot],
            )

        new_checklist = values.get("checklist_block", {}).get("checklist_input", {}).get("value", "")
        if new_checklist is not None:
            update_checklist_section(_heartbeat_file(), new_checklist)

    @app.action("heartbeat_toggle_on")
    async def action_hb_on(ack, respond, client):
        await ack()
        cfg = load_platform_config()
        cfg["heartbeat_enabled"] = True
        save_platform_config(cfg)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        from platforms.slack.utils import get_workspace_channels
        channels = await get_workspace_channels(client)
        await respond(blocks=_status_blocks(state, cfg, channels), text="Heartbeat ステータス", replace_original=True)

    @app.action("heartbeat_toggle_off")
    async def action_hb_off(ack, respond, client):
        await ack()
        cfg = load_platform_config()
        cfg["heartbeat_enabled"] = False
        save_platform_config(cfg)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        from platforms.slack.utils import get_workspace_channels
        channels = await get_workspace_channels(client)
        await respond(blocks=_status_blocks(state, cfg, channels), text="Heartbeat ステータス", replace_original=True)

    @app.action("heartbeat_thinking_on")
    async def action_hb_thinking_on(ack, respond, client):
        await ack()
        cfg = load_platform_config()
        cfg["heartbeat_thinking"] = True
        save_platform_config(cfg)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        from platforms.slack.utils import get_workspace_channels
        channels = await get_workspace_channels(client)
        await respond(blocks=_status_blocks(state, cfg, channels), text="Heartbeat ステータス", replace_original=True)

    @app.action("heartbeat_thinking_off")
    async def action_hb_thinking_off(ack, respond, client):
        await ack()
        cfg = load_platform_config()
        cfg["heartbeat_thinking"] = False
        save_platform_config(cfg)
        text = _read_heartbeat_text()
        state = parse_heartbeat_state(text)
        cfg = load_platform_config()
        from platforms.slack.utils import get_workspace_channels
        channels = await get_workspace_channels(client)
        await respond(blocks=_status_blocks(state, cfg, channels), text="Heartbeat ステータス", replace_original=True)


async def _reset_wrapup_done():
    update_heartbeat_state(_heartbeat_file(), "wrapup_done", "false")
    update_heartbeat_state(_heartbeat_file(), "last_updated", datetime.now(JST).strftime("%Y-%m-%d"))
    logger.info("Heartbeat: midnight reset, wrapup_done=false")
