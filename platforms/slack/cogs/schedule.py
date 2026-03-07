"""
Slack スケジュールハンドラ
- /schedule-ai add   : スケジュール追加
- /schedule-ai list  : スケジュール一覧
Discord ScheduleCog と同等のロジック
"""

from __future__ import annotations

import logging
import random
import string
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from core.config import (
    get_channel_name,
    load_platform_config,
    load_schedules,
    save_schedules,
    save_channel_name,
)
from core.scheduler import infer_freq_from_cron

if TYPE_CHECKING:
    from platforms.slack.bot import SlackBot

logger = logging.getLogger("slack_bot")

_DAY_MAP = {"月": "MON", "火": "TUE", "水": "WED", "木": "THU", "金": "FRI", "土": "SAT", "日": "SUN"}
_DAY_REV = {v: k for k, v in _DAY_MAP.items()}

_FREQ_LABELS = {
    "daily":    "毎日（時刻指定）",
    "weekday":  "平日のみ（月〜金）",
    "weekly":   "毎週（曜日・時刻指定）",
    "hourly":   "毎時（分指定）",
    "interval": "N分ごと",
}

JST = ZoneInfo("Asia/Tokyo")


def _parse_cron(freq: str, values: dict) -> str | None:
    try:
        if freq in ("daily", "weekday", "weekly"):
            h, m = values["time"].strip().split(":")
            h, m = int(h), int(m)
            if freq == "daily":
                cron = f"{m} {h} * * *"
            elif freq == "weekday":
                cron = f"{m} {h} * * MON-FRI"
            else:
                day = _DAY_MAP.get(values["day"].strip())
                if not day:
                    return None
                cron = f"{m} {h} * * {day}"
        elif freq == "hourly":
            m = int(values["minute"].strip())
            if not 0 <= m <= 59:
                return None
            cron = f"{m} * * * *"
        elif freq == "interval":
            n = int(values["interval"].strip())
            if n < 1:
                return None
            cron = f"*/{n} * * * *"
        else:
            return None
        CronTrigger.from_crontab(cron)
        return cron
    except Exception:
        return None


def _cron_to_fields(cron: str, freq: str) -> dict:
    try:
        m, h, dom, mon, dow = cron.strip().split()
        if freq in ("daily", "weekday"):
            return {"time": f"{int(h):02d}:{int(m):02d}"}
        if freq == "weekly":
            return {"time": f"{int(h):02d}:{int(m):02d}", "day": _DAY_REV.get(dow.upper(), "月")}
        if freq == "hourly":
            return {"minute": str(int(m))}
        if freq == "interval":
            return {"interval": m.removeprefix("*/")}
    except Exception:
        pass
    return {}


def _build_add_modal(
    channel_id: str,
    freq: str,
    model: str = "sonnet",
    thinking: bool = False,
    *,
    initial_values: dict | None = None,
    edit_id: str | None = None,
) -> dict:
    """スケジュール追加・編集モーダルの定義を生成する。"""
    iv = initial_values or {}

    def _inp(action_id: str, iv_key: str, **kwargs) -> dict:
        el: dict = {"type": "plain_text_input", "action_id": action_id, **kwargs}
        if iv.get(iv_key):
            el["initial_value"] = iv[iv_key]
        return el

    blocks = [
        {
            "type": "input",
            "block_id": "sched_name_block",
            "label": {"type": "plain_text", "text": "スケジュール名"},
            "element": _inp("sched_name_input", "name", max_length=100),
        },
        {
            "type": "input",
            "block_id": "sched_prompt_block",
            "label": {"type": "plain_text", "text": "実行プロンプト"},
            "element": _inp("sched_prompt_input", "prompt", multiline=True, max_length=3000),
        },
    ]

    if freq in ("daily", "weekday", "weekly"):
        blocks.append({
            "type": "input",
            "block_id": "sched_time_block",
            "label": {"type": "plain_text", "text": "実行時刻（HH:MM）"},
            "element": _inp("sched_time_input", "time", placeholder={"type": "plain_text", "text": "09:00"}, max_length=5),
        })
    if freq == "weekly":
        blocks.append({
            "type": "input",
            "block_id": "sched_day_block",
            "label": {"type": "plain_text", "text": "曜日（月火水木金土日）"},
            "element": _inp("sched_day_input", "day", placeholder={"type": "plain_text", "text": "月"}, max_length=1),
        })
    if freq == "hourly":
        blocks.append({
            "type": "input",
            "block_id": "sched_minute_block",
            "label": {"type": "plain_text", "text": "何分に実行（0〜59）"},
            "element": _inp("sched_minute_input", "minute", placeholder={"type": "plain_text", "text": "0"}, max_length=2),
        })
    if freq == "interval":
        blocks.append({
            "type": "input",
            "block_id": "sched_interval_block",
            "label": {"type": "plain_text", "text": "実行間隔（分）"},
            "element": _inp("sched_interval_input", "interval", placeholder={"type": "plain_text", "text": "30"}, max_length=3),
        })

    if edit_id:
        callback_id = f"schedule_edit_modal__{edit_id}__{freq}__{model}__{int(thinking)}"
        title_text = "スケジュール編集"
        submit_text = "保存"
    else:
        callback_id = f"schedule_add_modal__{channel_id}__{freq}__{model}__{int(thinking)}"
        title_text = "スケジュール追加"
        submit_text = "追加"

    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title_text},
        "submit": {"type": "plain_text", "text": submit_text},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "blocks": blocks,
    }


def _schedule_list_text(schedules: list[dict]) -> list[dict]:
    """スケジュール一覧の Block Kit ブロックを生成する。"""
    if not schedules:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "スケジュールはまだありません。"}}]

    blocks = []
    for s in schedules:
        if not s.get("id"):
            continue
        last_raw = s.get("last_run")
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw).astimezone(JST).strftime("%Y-%m-%d %H:%M")
            except Exception:
                last = last_raw
        else:
            last = "未実行"

        is_wrapup = s.get("type") == "wrapup"
        prompt_line = "（ラップアップ自動実行）" if is_wrapup else s["prompt"][:80]
        model_label = s.get("model", "sonnet")
        thinking_label = "ON" if s.get("thinking", s.get("mode") == "planning") else "OFF"
        ch_id = s["channel_id"]
        ch_name = get_channel_name(int(ch_id)) if ch_id.isdigit() else ch_id
        ch_ref = f"<#{ch_id}>" if ch_id else ch_name

        text = (
            f"*{s['name']}*\n"
            f"- Cron: `{s['cron']}`\n"
            f"- 内容: {prompt_line}\n"
            f"- チャンネル: {ch_ref}\n"
            f"- モデル: {model_label} / Thinking: {thinking_label}\n"
            f"- 状態: {s['status']}\n"
            f"- 実行回数: {s.get('run_count', 0)}\n"
            f"- 最終実行: {last}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({
            "type": "actions",
            "block_id": f"sched_actions__{s['id']}",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"sched_run__{s['id']}",
                    "text": {"type": "plain_text", "text": "今すぐ実行"},
                    "style": "primary",
                    "value": s["id"],
                },
                {
                    "type": "button",
                    "action_id": f"sched_edit__{s['id']}",
                    "text": {"type": "plain_text", "text": "編集"},
                    "value": s["id"],
                },
                {
                    "type": "button",
                    "action_id": f"sched_pause__{s['id']}",
                    "text": {"type": "plain_text", "text": "一時停止" if s["status"] == "active" else "再開"},
                    "value": s["id"],
                },
                {
                    "type": "button",
                    "action_id": f"sched_delete__{s['id']}",
                    "text": {"type": "plain_text", "text": "削除"},
                    "style": "danger",
                    "value": s["id"],
                },
            ],
        })
        blocks.append({"type": "divider"})
    return blocks


def register(bot: "SlackBot"):
    """スケジュールコマンドを Slack app に登録する。"""
    app = bot.app

    # ── /schedule-ai [add|list] ──────────────────────────────
    @app.command("/schedule-ai")
    async def cmd_schedule(ack, respond, command, client):
        await ack()
        text = (command.get("text") or "").strip().lower()
        channel_id = command.get("channel_id", "")
        channel_name = command.get("channel_name", channel_id)
        trigger_id = command.get("trigger_id", "")
        save_channel_name(channel_id, channel_name)

        if text == "list" or text == "":
            schedules = load_schedules()
            await respond(
                blocks=_schedule_list_text(schedules),
                text="スケジュール一覧",
                response_type="ephemeral",
            )
        elif text == "add":
            # チャンネル・頻度・モデル選択のセットアップ画面を表示
            from platforms.slack.utils import get_workspace_channels
            channels = await get_workspace_channels(client)
            options = [
                {"text": {"type": "plain_text", "text": f"# {name}"}, "value": ch_id}
                for ch_id, name in channels[:25]
            ]
            freq_options = [
                {"text": {"type": "plain_text", "text": v}, "value": k}
                for k, v in _FREQ_LABELS.items()
            ]
            from core.config import get_available_models
            model_options = []
            for m in get_available_models():
                if m == "sonnet": label = "Sonnet（高速）"
                elif m == "opus": label = "Opus（高精度）"
                elif m == "haiku": label = "Haiku（最速）"
                elif m == "gpt-5.4": label = "GPT-5.4（最新・最高精度）"
                elif m == "gpt-5.3": label = "GPT-5.3（高精度）"
                elif m == "gpt-5.2": label = "GPT-5.2（標準）"
                elif m == "gpt-5.1-max": label = "GPT-5.1 Max（大容量・高速）"
                elif m == "gpt-5.1-mini": label = "GPT-5.1 Mini（最速）"
                else: label = m
                model_options.append({"text": {"type": "plain_text", "text": label}, "value": m})
            thinking_options = [
                {"text": {"type": "plain_text", "text": "OFF（通常）"}, "value": "0"},
                {"text": {"type": "plain_text", "text": "ON（高精度・低速）"}, "value": "1"},
            ]
            await respond(
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*スケジュール追加*\n各項目を選択して「次へ」を押してください。"}},
                    {
                        "type": "actions",
                        "block_id": "sched_setup_channel",
                        "elements": [{
                            "type": "static_select",
                            "action_id": "sched_channel_select",
                            "placeholder": {"type": "plain_text", "text": "① 投稿先チャンネルを選択"},
                            "options": options,
                        }],
                    },
                    {
                        "type": "actions",
                        "block_id": "sched_setup_freq",
                        "elements": [{
                            "type": "static_select",
                            "action_id": "sched_freq_select",
                            "placeholder": {"type": "plain_text", "text": "② 実行頻度を選択"},
                            "options": freq_options,
                        }],
                    },
                    {
                        "type": "actions",
                        "block_id": "sched_setup_model",
                        "elements": [{
                            "type": "static_select",
                            "action_id": "sched_model_select",
                            "placeholder": {"type": "plain_text", "text": "③ モデルを選択"},
                            "options": model_options,
                        }],
                    },
                    {
                        "type": "actions",
                        "block_id": "sched_setup_thinking",
                        "elements": [{
                            "type": "static_select",
                            "action_id": "sched_thinking_select",
                            "placeholder": {"type": "plain_text", "text": "④ Thinkingモードを選択"},
                            "options": thinking_options,
                        }],
                    },
                    {
                        "type": "actions",
                        "block_id": "sched_setup_next",
                        "elements": [{
                            "type": "button",
                            "action_id": "sched_next",
                            "text": {"type": "plain_text", "text": "⑤ 次へ →"},
                            "style": "primary",
                        }],
                    },
                ],
                text="スケジュール追加",
                response_type="ephemeral",
            )
        else:
            await respond(
                text="使用方法: `/schedule-ai add` または `/schedule-ai list`",
                response_type="ephemeral",
            )

    # ── セットアップ画面のアクション ──────────────────────────
    # 選択値を private_metadata に渡すため、ボタン押下時にモーダルを開く
    # ただし Block Kit では中間状態を保持できないため、ボタン value に埋め込む方式を使う

    @app.action("sched_channel_select")
    async def action_sched_channel(ack):
        await ack()

    @app.action("sched_freq_select")
    async def action_sched_freq(ack):
        await ack()

    @app.action("sched_model_select")
    async def action_sched_model(ack):
        await ack()

    @app.action("sched_thinking_select")
    async def action_sched_thinking(ack):
        await ack()

    @app.action("sched_next")
    async def action_sched_next(ack, body, client):
        await ack()
        trigger_id = body.get("trigger_id", "")
        state = body.get("state", {}).get("values", {})

        ch_id = (
            state.get("sched_setup_channel", {})
            .get("sched_channel_select", {})
            .get("selected_option", {})
            .get("value", "")
        )
        freq = (
            state.get("sched_setup_freq", {})
            .get("sched_freq_select", {})
            .get("selected_option", {})
            .get("value", "daily")
        )
        model = (
            state.get("sched_setup_model", {})
            .get("sched_model_select", {})
            .get("selected_option", {})
            .get("value", "sonnet")
        )
        thinking_val = (
            state.get("sched_setup_thinking", {})
            .get("sched_thinking_select", {})
            .get("selected_option", {})
            .get("value", "0")
        )
        thinking = thinking_val == "1"

        if not ch_id:
            await client.views_open(
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "エラー"},
                    "close": {"type": "plain_text", "text": "閉じる"},
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": ":warning: チャンネルを選択してください。"}}],
                },
            )
            return

        modal = _build_add_modal(ch_id, freq, model, thinking)
        await client.views_open(trigger_id=trigger_id, view=modal)

    # ── スケジュール追加モーダルの submit ──────────────────────
    @app.view(re_pattern(r"^schedule_add_modal__"))
    async def handle_schedule_add_modal(ack, body, view, client):
        await ack()
        callback_id = view["callback_id"]
        parts = callback_id.split("__")
        ch_id = parts[1] if len(parts) > 1 else ""
        freq = parts[2] if len(parts) > 2 else "daily"
        model = parts[3] if len(parts) > 3 else "sonnet"
        thinking = parts[4] == "1" if len(parts) > 4 else False

        state = view["state"]["values"]

        def _val(block_id: str, action_id: str) -> str:
            return state.get(block_id, {}).get(action_id, {}).get("value", "") or ""

        name = _val("sched_name_block", "sched_name_input").strip()
        prompt = _val("sched_prompt_block", "sched_prompt_input").strip()

        cron_values: dict[str, str] = {}
        if freq in ("daily", "weekday", "weekly"):
            cron_values["time"] = _val("sched_time_block", "sched_time_input")
        if freq == "weekly":
            cron_values["day"] = _val("sched_day_block", "sched_day_input")
        if freq == "hourly":
            cron_values["minute"] = _val("sched_minute_block", "sched_minute_input")
        if freq == "interval":
            cron_values["interval"] = _val("sched_interval_block", "sched_interval_input")

        cron = _parse_cron(freq, cron_values)
        if not cron:
            # エラーはモーダル validation で返せないため DM で通知
            user_id = body.get("user", {}).get("id", "")
            if user_id:
                await client.chat_postMessage(channel=user_id, text=":warning: cron 式の生成に失敗しました。入力値を確認してください。")
            return

        new_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        schedules = load_schedules()
        if schedules is None:
            schedules = []
        schedules.append({
            "id": new_id,
            "name": name,
            "cron": cron,
            "prompt": prompt,
            "channel_id": ch_id,
            "model": model,
            "thinking": thinking,
            "status": "active",
            "run_count": 0,
            "last_run": None,
        })
        save_schedules(schedules)
        bot._reload_schedules()

        user_id = body.get("user", {}).get("id", "")
        thinking_label = "ON" if thinking else "OFF"
        msg = (
            f":white_check_mark: スケジュール *{name}* を追加しました\n"
            f"- Cron: `{cron}`\n"
            f"- モデル: {model} / Thinking: {thinking_label}\n"
            f"- チャンネル: <#{ch_id}>"
        )
        if user_id:
            await client.chat_postMessage(channel=user_id, text=msg)

    # ── スケジュール編集 ────────────────────────────────────
    @app.action(re_pattern(r"^sched_edit__"))
    async def action_sched_edit(ack, body, client):
        await ack()
        action_id = body["actions"][0]["action_id"]
        sched_id = action_id.replace("sched_edit__", "")
        trigger_id = body.get("trigger_id", "")
        schedules = load_schedules()
        if schedules is None:
            user_id = body.get("user", {}).get("id", "")
            if user_id:
                await client.chat_postMessage(channel=user_id, text="⚠️ スケジュールデータが破損しています。")
            return
        target = next((s for s in schedules if s.get("id") == sched_id), None)
        if not target:
            return
        freq = infer_freq_from_cron(target.get("cron", "0 9 * * *"))
        fields = _cron_to_fields(target.get("cron", ""), freq)
        initial_values = {
            "name": target.get("name", ""),
            "prompt": target.get("prompt", ""),
            **fields,
        }
        modal = _build_add_modal(
            target["channel_id"],
            freq,
            target.get("model", "sonnet"),
            target.get("thinking", False),
            initial_values=initial_values,
            edit_id=sched_id,
        )
        await client.views_open(trigger_id=trigger_id, view=modal)

    @app.view(re_pattern(r"^schedule_edit_modal__"))
    async def handle_schedule_edit_modal(ack, body, view, client):
        await ack()
        callback_id = view["callback_id"]
        parts = callback_id.split("__")
        sched_id = parts[1] if len(parts) > 1 else ""
        freq = parts[2] if len(parts) > 2 else "daily"
        model = parts[3] if len(parts) > 3 else "sonnet"
        thinking = parts[4] == "1" if len(parts) > 4 else False

        state = view["state"]["values"]

        def _val(block_id: str, action_id: str) -> str:
            return state.get(block_id, {}).get(action_id, {}).get("value", "") or ""

        name = _val("sched_name_block", "sched_name_input").strip()
        prompt = _val("sched_prompt_block", "sched_prompt_input").strip()

        cron_values: dict[str, str] = {}
        if freq in ("daily", "weekday", "weekly"):
            cron_values["time"] = _val("sched_time_block", "sched_time_input")
        if freq == "weekly":
            cron_values["day"] = _val("sched_day_block", "sched_day_input")
        if freq == "hourly":
            cron_values["minute"] = _val("sched_minute_block", "sched_minute_input")
        if freq == "interval":
            cron_values["interval"] = _val("sched_interval_block", "sched_interval_input")

        cron = _parse_cron(freq, cron_values)
        if not cron:
            user_id = body.get("user", {}).get("id", "")
            if user_id:
                await client.chat_postMessage(channel=user_id, text=":warning: cron 式の生成に失敗しました。入力値を確認してください。")
            return

        schedules = load_schedules()
        if schedules is None:
            user_id = body.get("user", {}).get("id", "")
            if user_id:
                await client.chat_postMessage(channel=user_id, text="⚠️ スケジュールデータが破損しています。")
            return
        for s in schedules:
            if s.get("id") == sched_id:
                s["name"] = name
                s["prompt"] = prompt
                s["cron"] = cron
                s["model"] = model
                s["thinking"] = thinking
                break
        save_schedules(schedules)
        bot._reload_schedules()

        user_id = body.get("user", {}).get("id", "")
        if user_id:
            thinking_label = "ON" if thinking else "OFF"
            await client.chat_postMessage(
                channel=user_id,
                text=(
                    f":white_check_mark: スケジュール *{name}* を更新しました\n"
                    f"- Cron: `{cron}`\n"
                    f"- モデル: {model} / Thinking: {thinking_label}"
                ),
            )

    # ── スケジュール一覧のアクション ──────────────────────────
    @app.action(re_pattern(r"^sched_run__"))
    async def action_sched_run(ack, body, client, respond):
        await ack()
        action_id = body["actions"][0]["action_id"]
        sched_id = action_id.replace("sched_run__", "")
        schedules = load_schedules()
        if schedules is None:
            await respond(text="⚠️ スケジュールデータが破損しています。")
            return
        target = next((s for s in schedules if s["id"] == sched_id), None)
        if not target:
            await respond(text=":warning: スケジュールが見つかりません。", replace_original=False)
            return
        await bot._run_schedule(target, client)
        await respond(text=f":white_check_mark: *{target['name']}* を実行しました。", replace_original=False)

    @app.action(re_pattern(r"^sched_pause__"))
    async def action_sched_pause(ack, body, respond):
        await ack()
        action_id = body["actions"][0]["action_id"]
        sched_id = action_id.replace("sched_pause__", "")
        schedules = load_schedules()
        if schedules is None:
            await respond(text="⚠️ スケジュールデータが破損しています。", replace_original=False)
            return
        name = ""
        for s in schedules:
            if s["id"] == sched_id:
                name = s["name"]
                s["status"] = "paused" if s["status"] == "active" else "active"
        save_schedules(schedules)
        bot._reload_schedules()
        await respond(text=f":white_check_mark: *{name}* の状態を切り替えました。", replace_original=False)

    @app.action(re_pattern(r"^sched_delete__"))
    async def action_sched_delete(ack, body, respond):
        await ack()
        action_id = body["actions"][0]["action_id"]
        sched_id = action_id.replace("sched_delete__", "")
        schedules = load_schedules()
        if schedules is None:
            await respond(text="⚠️ スケジュールデータが破損しています。", replace_original=False)
            return
        target = next((s for s in schedules if s["id"] == sched_id), None)
        name = target["name"] if target else sched_id
        schedules = [s for s in schedules if s["id"] != sched_id]
        save_schedules(schedules)
        bot._reload_schedules()
        await respond(text=f":white_check_mark: *{name}* を削除しました。", replace_original=False)


def re_pattern(pattern: str):
    """Bolt の action/view マッチング用の正規表現オブジェクトを返す。"""
    import re
    return re.compile(pattern)
