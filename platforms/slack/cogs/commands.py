"""
Slack スラッシュコマンドハンドラ
- /model   : モデル・Thinking 設定
- /status  : 現在のステータス表示
- /cancel  : 実行中タスクのキャンセル
- /mention : チャンネルのメンション要否設定
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING

from core.config import (
    load_platform_config,
    save_platform_config,
    get_model_config,
    get_no_mention_channels,
    set_no_mention,
    get_channel_session,
    save_channel_session,
    delete_channel_session,
    BASE_DIR,
)
from core.engine import run_engine


def _status_blocks(model: str, thinking: bool, running: bool, reply_in_thread: bool) -> list[dict]:
    thinking_label = "ON" if thinking else "OFF"
    thread_label = "ON" if reply_in_thread else "OFF"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*ステータス*\n"
                    f"- モデル: *{model}*\n"
                    f"- Thinking: *{thinking_label}*\n"
                    f"- Claude 実行中: *{'はい' if running else 'いいえ'}*\n"
                    f"- スレッド返信: *{thread_label}*"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "thread_reply_block",
            "elements": [
                _btn("thread_reply_on", "スレッド返信 ON", primary=reply_in_thread),
                _btn("thread_reply_off", "スレッド返信 OFF", primary=not reply_in_thread),
            ],
        },
    ]

if TYPE_CHECKING:
    from platforms.slack.bot import SlackBot

logger = logging.getLogger("slack_bot")

_MODEL_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Sonnet（高速・バランス型）"}, "value": "sonnet"},
    {"text": {"type": "plain_text", "text": "Opus（最高精度・低速）"}, "value": "opus"},
    {"text": {"type": "plain_text", "text": "Haiku（最速・軽量）"}, "value": "haiku"},
]


def _btn(action_id: str, text: str, *, primary: bool = False) -> dict:
    b: dict = {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}}
    if primary:
        b["style"] = "primary"
    return b


def _model_blocks(model: str, thinking: bool) -> list[dict]:
    thinking_label = "ON" if thinking else "OFF"
    matched = next((o for o in _MODEL_OPTIONS if o["value"] == model), None)
    select: dict = {
        "type": "static_select",
        "action_id": "model_select",
        "placeholder": {"type": "plain_text", "text": "モデルを選択"},
        "options": _MODEL_OPTIONS,
    }
    if matched:
        select["initial_option"] = matched
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*モデル設定*\n現在: *{model}* / Thinking: *{thinking_label}*",
            },
        },
        {
            "type": "actions",
            "block_id": "model_select_block",
            "elements": [select],
        },
        {
            "type": "actions",
            "block_id": "thinking_block",
            "elements": [
                _btn("thinking_on", "Thinking ON", primary=thinking),
                _btn("thinking_off", "Thinking OFF", primary=not thinking),
            ],
        },
    ]


def register(bot: "SlackBot"):
    """スラッシュコマンドを Slack app に登録する。"""
    app = bot.app

    # ── /model ──────────────────────────────────────────
    @app.command("/model-ai")
    async def cmd_model(ack, command, client):
        await ack()
        model, thinking = get_model_config()
        await client.chat_postEphemeral(
            channel=command["channel_id"],
            user=command["user_id"],
            blocks=_model_blocks(model, thinking),
            text="モデル設定",
        )

    @app.action("model_select")
    async def action_model_select(ack, body, respond):
        await ack()
        selected = body["actions"][0]["selected_option"]["value"]
        cfg = load_platform_config()
        cfg["model"] = selected
        save_platform_config(cfg)
        model, thinking = get_model_config()
        await respond(blocks=_model_blocks(model, thinking), text="モデル設定", replace_original=True)

    @app.action("thinking_on")
    async def action_thinking_on(ack, respond):
        await ack()
        cfg = load_platform_config()
        cfg["thinking"] = True
        save_platform_config(cfg)
        model, thinking = get_model_config()
        await respond(blocks=_model_blocks(model, thinking), text="モデル設定", replace_original=True)

    @app.action("thinking_off")
    async def action_thinking_off(ack, respond):
        await ack()
        cfg = load_platform_config()
        cfg["thinking"] = False
        save_platform_config(cfg)
        model, thinking = get_model_config()
        await respond(blocks=_model_blocks(model, thinking), text="モデル設定", replace_original=True)

    # ── /status ─────────────────────────────────────────
    @app.command("/status-ai")
    async def cmd_status(ack, respond):
        await ack()
        model, thinking = get_model_config()
        running = bool(bot.running_tasks)
        cfg = load_platform_config()
        reply_in_thread = cfg.get("reply_in_thread", True)
        await respond(
            blocks=_status_blocks(model, thinking, running, reply_in_thread),
            text="ステータス",
            response_type="ephemeral",
        )

    @app.action("thread_reply_on")
    async def action_thread_reply_on(ack, respond):
        await ack()
        cfg = load_platform_config()
        cfg["reply_in_thread"] = True
        save_platform_config(cfg)
        model, thinking = get_model_config()
        await respond(
            blocks=_status_blocks(model, thinking, bool(bot.running_tasks), True),
            text="ステータス",
            replace_original=True,
        )

    @app.action("thread_reply_off")
    async def action_thread_reply_off(ack, respond):
        await ack()
        cfg = load_platform_config()
        cfg["reply_in_thread"] = False
        save_platform_config(cfg)
        model, thinking = get_model_config()
        await respond(
            blocks=_status_blocks(model, thinking, bool(bot.running_tasks), False),
            text="ステータス",
            replace_original=True,
        )

    # ── /cancel ─────────────────────────────────────────
    @app.command("/cancel-ai")
    async def cmd_cancel(ack, respond, command):
        await ack()
        channel_id = command.get("channel_id", "")
        task = bot.running_tasks.get(channel_id)
        if task and not task.done():
            proc = bot.running_processes.get(channel_id)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            task.cancel()
            await respond(text=":white_check_mark: タスクをキャンセルしました。", response_type="ephemeral")
        else:
            await respond(text=":information_source: 実行中のタスクはありません。", response_type="ephemeral")

    # ── /reset ─────────────────────────────────────────
    @app.command("/reset-ai")
    async def cmd_reset(ack, respond, command):
        await ack()
        channel_id = command.get("channel_id", "")
        task = bot.running_tasks.get(channel_id)
        if task and not task.done():
            proc = bot.running_processes.get(channel_id)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            task.cancel()
        deleted = delete_channel_session(channel_id)
        if deleted:
            await respond(text=":white_check_mark: セッションをリセットしました。次のメッセージから新しい会話が始まります。", response_type="ephemeral")
        else:
            await respond(text=":information_source: このチャンネルにはアクティブなセッションがありません。", response_type="ephemeral")

    # ── /mention ─────────────────────────────────────────
    @app.command("/mention-ai")
    async def cmd_mention(ack, command, client):
        await ack()
        channel_id = command.get("channel_id", "")
        user_id = command.get("user_id", "")
        current = channel_id in get_no_mention_channels()
        status = "メンション不要（全メッセージに応答）" if current else "メンション必要（@メンションのみ応答）"
        await client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*メンション設定*\n現在: {status}"},
                },
                {
                    "type": "actions",
                    "block_id": "mention_block",
                    "elements": [
                        {**_btn("mention_off", "メンション不要", primary=current), "value": channel_id},
                        {**_btn("mention_on", "メンション必要", primary=not current), "value": channel_id},
                    ],
                },
            ],
            text="メンション設定",
        )

    @app.action("mention_off")
    async def action_mention_off(ack, body, respond):
        await ack()
        channel_id = body["actions"][0]["value"]
        set_no_mention(channel_id, True)
        await respond(text=":white_check_mark: このチャンネルはメンション不要になりました。", replace_original=True)

    @app.action("mention_on")
    async def action_mention_on(ack, body, respond):
        await ack()
        channel_id = body["actions"][0]["value"]
        set_no_mention(channel_id, False)
        await respond(text=":white_check_mark: このチャンネルはメンション必要になりました。", replace_original=True)

    # ── /skills-list ─────────────────────────────────────
    @app.command("/skills-list")
    async def cmd_skills_list(ack, command, client):
        await ack()
        import asyncio
        await asyncio.to_thread(bot.skill_registry.reload, BASE_DIR / "skills")
        skills = [s for s in bot.skill_registry.all_skills() if s.user_invocable]
        channel_id = command["channel_id"]
        user_id = command["user_id"]

        errors = bot.skill_registry.load_errors

        if not skills and not errors:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id, text=":information_source: 利用可能なスキルがありません。"
            )
            return

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*利用可能なスキル*"}},
            {"type": "divider"},
        ]
        if not skills:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "現在利用可能なスキルはありません。"}})

        if errors:
            err_text = ""
            for p, err in errors:
                err_text += f"• `{p.parent.name}`: {err}\n"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*⚠️ 読み込みエラーのスキル*\n{err_text}"}})
            blocks.append({"type": "divider"})

        for skill in skills:
            desc = skill.description[:80] + "…" if len(skill.description) > 80 else skill.description
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{skill.name}*\n{desc}"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "発動"},
                    "action_id": f"skill_invoke__{skill.name}",
                    "value": f"{channel_id}|{skill.name}",
                },
            })
        await client.chat_postEphemeral(
            channel=channel_id, user=user_id, blocks=blocks, text="スキル一覧"
        )

    @app.action(re.compile(r"skill_invoke__.*"))
    async def action_skill_invoke(ack, body, client, action):
        await ack()
        value = action.get("value", "")
        parts = value.split("|", 1)
        if len(parts) != 2:
            return
        channel_id, skill_name = parts

        platform_cfg = load_platform_config()
        reply_in_thread = platform_cfg.get("reply_in_thread", True)

        try:
            await client.reactions_add(channel=channel_id, name="thinking_face", timestamp=body.get("message", {}).get("ts", ""))
        except Exception:
            pass

        try:
            lock = bot.get_channel_lock(channel_id)
            async with lock:
                session_id = get_channel_session(channel_id)
                is_new = session_id is None
                if is_new:
                    session_id = str(uuid.uuid4())
                    save_channel_session(channel_id, session_id)

                task = asyncio.current_task()
                bot.running_tasks[channel_id] = task

                model, thinking = get_model_config()
                registry_instr = bot.skill_registry.build_instructions(
                    bot.platform_context.name,
                    disabled=bot.platform_context.disabled_skills,
                )
                skill_instr = (
                    f"[platform: {bot.platform_context.name}]\n"
                    + (f"\n{bot.platform_context.format_hint}\n" if bot.platform_context.format_hint else "")
                    + (f"\n{registry_instr}" if registry_instr else "")
                )
                prompt = f"[{skill_name}スキルを呼び出してください。スキルの指示に従って会話を開始してください。]"
                response, timed_out = await run_engine(
                    prompt,
                    model=model,
                    thinking=thinking,
                    session_id=session_id,
                    is_new_session=is_new,
                    on_process=lambda p: bot.running_processes.__setitem__(channel_id, p),
                    skill_instructions=skill_instr,
                )

            if timed_out:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=":warning: タイムアウトしました。`/cancel-ai` で再試行するか、少し待ってから再送してください。",
                )
                return

            if response:
                await client.chat_postMessage(channel=channel_id, text=response)
        finally:
            bot.running_tasks.pop(channel_id, None)
            bot.running_processes.pop(channel_id, None)
            try:
                ts = body.get("message", {}).get("ts", "")
                if ts:
                    await client.reactions_remove(channel=channel_id, name="thinking_face", timestamp=ts)
            except Exception:
                pass
