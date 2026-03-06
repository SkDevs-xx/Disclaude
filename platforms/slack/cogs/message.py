"""
Slack メッセージハンドラ
- app_mention: チャンネルでの @メンション
- message (im): ダイレクトメッセージ
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import (
    load_platform_config,
    get_channel_session,
    save_channel_session,
    save_channel_name,
    get_model_config,
    get_no_mention_channels,
)
from core.engine import run_engine
from core.message import split_message
from core.attachments import process_attachment

if TYPE_CHECKING:
    from platforms.slack.bot import SlackBot

logger = logging.getLogger("slack_bot")


async def _download_slack_file(url: str, token: str) -> bytes | None:
    """Slack のプライベートファイルをダウンロードする（Bearer 認証付き）。"""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.warning("Slack file download error: %s", e)
    return None


async def handle_claude_message(
    bot: "SlackBot",
    channel_id: str,
    channel_name: str,
    user_id: str,
    text: str,
    thread_ts: str | None,
    files: list[dict] | None,
    say,
    client,
):
    """Claude に問い合わせて返信する共通処理。"""
    platform_cfg = load_platform_config()
    allowed = platform_cfg.get("allowed_user_ids", [])
    if allowed and user_id not in allowed:
        logger.info("[message] skipped: user %s not in allowlist", user_id)
        return

    save_channel_name(channel_id, channel_name)

    # ファイル添付処理
    injected_text = ""
    image_paths: list[Path] = []
    if files:
        bot_token = bot._bot_token
        for f in files:
            filename = f.get("name", "file")
            url = f.get("url_private", "")
            content_type = f.get("mimetype", "application/octet-stream")
            size = f.get("size", 0)

            if not url:
                continue

            # ファイルをダウンロードして一時保存
            data = await _download_slack_file(url, bot_token)
            if data is None:
                continue

            import core.config as _cfg
            tmp_path = _cfg.TMP_DIR / filename
            tmp_path.write_bytes(data)

            # process_attachment に渡せる形式のラッパーを作成
            class _FileObj:
                pass

            file_obj = _FileObj()
            file_obj.filename = filename
            file_obj.url = url
            file_obj.content_type = content_type
            file_obj.size = size
            # process_attachment はダウンロード済みファイルを期待しているため
            # tmp_path を直接参照するアダプタを使う
            file_obj._local_path = tmp_path

            text_part, image_path = await _process_local_file(file_obj)
            if text_part:
                injected_text += text_part
            if image_path is not None:
                image_paths.append(image_path)

    user_text = re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()

    if not user_text and not injected_text:
        return

    full_prompt = user_text + injected_text

    # 返信先スレッドを決める
    reply_in_thread = platform_cfg.get("reply_in_thread", True)
    reply_ts = (thread_ts if thread_ts else None) if reply_in_thread else None

    # 処理中リアクション
    try:
        msg_ts = thread_ts or ""
        await client.reactions_add(channel=channel_id, name="thinking_face", timestamp=msg_ts)
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
            response, timed_out = await run_engine(
                full_prompt,
                model=model,
                thinking=thinking,
                session_id=session_id,
                is_new_session=is_new,
                on_process=lambda p: bot.running_processes.__setitem__(channel_id, p),
                skill_instructions=skill_instr,
            )

            bot.running_tasks.pop(channel_id, None)
            bot.running_processes.pop(channel_id, None)

        if timed_out:
            await say(
                text=":warning: タイムアウトしました。`/cancel` で再試行するか、少し待ってから再送してください。",
                channel=channel_id,
                thread_ts=reply_ts,
            )
            return

        display_response = re.sub(r"\n{3,}", "\n\n", response)
        chunks = split_message(display_response, max_len=3000)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await say(text=chunk, channel=channel_id, thread_ts=reply_ts)
            else:
                await say(text=chunk, channel=channel_id, thread_ts=reply_ts)

    finally:
        # リアクション削除
        try:
            if msg_ts:
                await client.reactions_remove(channel=channel_id, name="thinking_face", timestamp=msg_ts)
        except Exception:
            pass
        # 画像ファイルをクリーンアップ
        for p in image_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                logger.warning("画像クリーンアップ失敗: %s", p)


async def _process_local_file(file_obj) -> tuple[str | None, Path | None]:
    """ローカルに保存済みの Slack ファイルを処理する。"""
    import core.config as _cfg

    local_path: Path = file_obj._local_path
    filename = file_obj.filename
    content_type = file_obj.content_type or ""
    ext = local_path.suffix.lower()

    TEXT_EXTENSIONS = {
        ".txt", ".csv", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
        ".html", ".css", ".sh", ".toml", ".ini", ".env", ".xml", ".log",
    }
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    if ext in TEXT_EXTENSIONS or content_type.startswith("text/"):
        try:
            content = local_path.read_text(encoding="utf-8", errors="replace")
            local_path.unlink(missing_ok=True)
            return f"\n\n--- Attachment: {filename} ---\n{content[:4000]}\n---\n", None
        except Exception as e:
            logger.warning("テキストファイル読み込み失敗: %s", e)
            local_path.unlink(missing_ok=True)
            return None, None
    elif ext in IMAGE_EXTENSIONS or content_type.startswith("image/"):
        tmp_path = _cfg.TMP_DIR / filename
        if not tmp_path.exists():
            local_path.rename(tmp_path)
        return f"\n\n（添付画像: {tmp_path}）\n", tmp_path
    else:
        local_path.unlink(missing_ok=True)
        return None, None


def register(bot: "SlackBot"):
    """メッセージハンドラを Slack app に登録する。"""
    app = bot.app

    @app.event("app_mention")
    async def on_app_mention(event, say, client):
        channel_id = event.get("channel", "")
        channel_name = event.get("channel_name") or channel_id
        user_id = event.get("user", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts", "")
        files = event.get("files", [])

        # thread_ts がない場合はメッセージ自体の ts をスレッド起点にする
        reply_ts = thread_ts or message_ts

        await handle_claude_message(
            bot=bot,
            channel_id=channel_id,
            channel_name=channel_name,
            user_id=user_id,
            text=text,
            thread_ts=reply_ts,
            files=files,
            say=say,
            client=client,
        )

    @app.event("message")
    async def on_direct_message(event, say, client):
        subtype = event.get("subtype")
        if subtype:
            return

        channel_type = event.get("channel_type")
        channel_id = event.get("channel", "")
        no_mention_channels = get_no_mention_channels()

        # DM またはメンション不要チャンネルのみ処理
        is_dm = channel_type == "im"
        is_no_mention = channel_id in no_mention_channels
        logger.info("[message] ch=%s type=%s is_dm=%s is_no_mention=%s no_mention_set=%s",
                    channel_id, channel_type, is_dm, is_no_mention, no_mention_channels)
        if not is_dm and not is_no_mention:
            return

        user_id = event.get("user", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts", "")
        files = event.get("files", [])

        reply_ts = thread_ts or message_ts
        channel_name = "DM" if is_dm else (event.get("channel_name") or channel_id)

        await handle_claude_message(
            bot=bot,
            channel_id=channel_id,
            channel_name=channel_name,
            user_id=user_id,
            text=text,
            thread_ts=reply_ts,
            files=files,
            say=say,
            client=client,
        )
