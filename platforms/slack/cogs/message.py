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


import aiofiles
from core.attachments import MAX_ATTACHMENT_SIZE

async def _download_slack_file_to_path(url: str, token: str, save_path: Path, session=None) -> bool:
    """Slack のプライベートファイルをダウンロードして一時ファイルに保存する（Bearer 認証付き）。

    OOM 防御のため iter_chunked で書き出し、最大 10MB までとする。
    Returns:
        bool: 成功時に True
    """
    import aiohttp
    
    async def _fetch_and_save(s):
        async with s.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status != 200:
                logger.warning("Slack file download failed, status: %d", resp.status)
                return False
                
            # Content-Length check early
            cl = resp.headers.get("Content-Length")
            if cl and int(cl) > MAX_ATTACHMENT_SIZE:
                logger.warning("Slack file too large: %s bytes", cl)
                return False
                
            downloaded = 0
            async with aiofiles.open(save_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_ATTACHMENT_SIZE:
                        logger.warning("Slack file exceeded max size during download")
                        return False
                    await f.write(chunk)
            return True

    try:
        if session is not None:
            return await _fetch_and_save(session)
        else:
            async with aiohttp.ClientSession() as s:
                return await _fetch_and_save(s)
    except Exception as e:
        logger.warning("Slack file download error: %s", e)
    return False


async def handle_clive_message(
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
    """Clive に問い合わせて返信する共通処理。"""
    platform_cfg = load_platform_config()
    allowed = platform_cfg.get("allowed_user_ids", [])
    if allowed and user_id not in allowed:
        logger.info("[message] skipped: user %s not in allowlist", user_id)
        return

    save_channel_name(channel_id, channel_name)

    # ファイル添付処理
    # 処理中リアクション
    try:
        msg_ts = thread_ts or ""
        await client.reactions_add(channel=channel_id, name="thinking_face", timestamp=msg_ts)
    except Exception:
        pass

    try:
        # ファイル添付処理
        injected_text = ""
        image_paths: list[Path] = []
        if files:
            import aiohttp
            bot_token = bot._bot_token
            async with aiohttp.ClientSession() as dl_session:
                for f in files:
                    filename = f.get("name", "file")
                    url = f.get("url_private", "")
                    content_type = f.get("mimetype", "application/octet-stream")
                    size = f.get("size", 0)

                    if not url:
                        continue

                    import core.config as _cfg
                    safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
                    tmp_path = _cfg.TMP_DIR / safe_name
                    _cfg.TMP_DIR.mkdir(parents=True, exist_ok=True)
                    
                    # ファイルをダウンロードして一時保存 (OOM回避のためストリーミング)
                    success = await _download_slack_file_to_path(url, bot_token, tmp_path, session=dl_session)
                    if not success:
                        tmp_path.unlink(missing_ok=True)
                        injected_text += f"\n\n（添付ファイル: {filename} — サイズ超過またはダウンロード失敗）\n"
                        continue

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

        full_prompt = ""
        if user_text:
            full_prompt += f"<user_input>\n{user_text}\n</user_input>\n"
        if injected_text:
            full_prompt += f"<attachments>\n{injected_text}\n</attachments>\n"
        full_prompt = full_prompt.strip()

        lock = bot.get_channel_lock(channel_id)
        async with lock:
            session_id = get_channel_session(channel_id)
            is_new = session_id is None

            task = asyncio.current_task()
            bot.running_tasks[channel_id] = task

            model, thinking = get_model_config()
            registry_instr = bot.skill_registry.build_instructions(
                bot.platform_context.name,
                disabled=bot.platform_context.disabled_skills,
                exclude_user_invocable=True,
            )
            skill_instr = (
                (f"{bot.platform_context.format_hint}\n" if bot.platform_context.format_hint else "")
                + (f"{registry_instr}\n" if registry_instr else "")
            )
            response, timed_out, new_session_id = await run_engine(
                full_prompt,
                model=model,
                thinking=thinking,
                session_id=session_id,
                is_new_session=is_new,
                on_process=lambda p: bot.running_processes.__setitem__(channel_id, p),
                skill_instructions=skill_instr,
                platform_name=bot.platform_context.name,
            )
            
            if is_new and new_session_id:
                save_channel_session(channel_id, new_session_id)

            bot.running_tasks.pop(channel_id, None)
            bot.running_processes.pop(channel_id, None)

        if timed_out:
            await say(
                text=":warning: タイムアウトしました。`/cancel` で再試行するか、少し待ってから再送してください。",
                channel=channel_id,
                thread_ts=thread_ts,
            )
            return

        display_response = re.sub(r"\n{3,}", "\n\n", response)
        chunks = split_message(display_response, max_len=3000)
        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(1.2)  # Rate Limit 回避
            await say(text=chunk, channel=channel_id, thread_ts=thread_ts)

    finally:
        bot.running_tasks.pop(channel_id, None)
        bot.running_processes.pop(channel_id, None)
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

        await handle_clive_message(
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

        await handle_clive_message(
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
