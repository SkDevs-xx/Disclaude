"""
Slack /summarize-ai ハンドラ
チャンネルの会話を 2 段階 Claude 呼び出しで要約・質問に回答する

処理フロー:
  1. Slack API でチャンネルの全メッセージを収集して tmp ファイルに書き出す
  2. Stage 1: ファイルの先頭・末尾サンプル + プロンプト → Claude が検索条件（JSON）を返す
  3. Python: ファイル全体をキーワード/日付でフィルタリング
  4. Stage 2: 絞り込んだ行を Claude に渡して最終回答を生成
  5. finally: tmp ファイルを必ず削除
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import core.config as _cfg
from core.claude import run_claude
from core.message import split_message

if TYPE_CHECKING:
    from platforms.slack.bot import SlackBot

logger = logging.getLogger("slack_bot")
JST = ZoneInfo("Asia/Tokyo")

_user_cache: dict[str, str] = {}


async def _resolve_user(client, user_id: str) -> str:
    """ユーザーIDを表示名に解決する（キャッシュ付き）。"""
    if not user_id or not user_id.startswith("U"):
        return user_id
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        info = await client.users_info(user=user_id)
        profile = info["user"]["profile"]
        name = profile.get("display_name") or profile.get("real_name") or user_id
        _user_cache[user_id] = name
    except Exception:
        _user_cache[user_id] = user_id
    return _user_cache[user_id]

CHAR_LIMIT = 600_000
FETCH_CHAR_CAP = 2_000_000
SAMPLE_HEAD_CHARS = 3_000
SAMPLE_TAIL_CHARS = 3_000


async def _get_search_criteria(
    bot: "SlackBot",
    question: str,
    sample_text: str,
    channel_id: str,
) -> dict:
    """Stage 1: メッセージサンプル + プロンプトから検索条件を Claude で抽出する。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta_prompt = (
        f"以下は Slack チャンネルの会話ログのサンプルです。\n\n"
        f"=== 先頭サンプル ===\n{sample_text}\n\n"
        f"上記ログに関して、次の指示を実行するための検索条件をJSONで返してください。\n"
        f"指示: 「{question}」\n"
        f"今日: {today}\n\n"
        "以下のJSON形式のみで回答してください（コードブロック不要）:\n"
        "{\n"
        '  "use_all": true,\n'
        '  "keywords": [],\n'
        '  "date_from": null,\n'
        '  "date_to": null\n'
        "}\n\n"
        "- 「まとめて」「要約して」など全体対象なら use_all: true、keywords: []\n"
        "- 特定トピックなら use_all: false、keywords に検索ワードを設定\n"
        "- 「先週」「3日前」など時期指定があれば date_from / date_to を設定\n"
        "- 「先週」なら date_from を7日前、date_to を昨日に設定"
    )

    lock = bot.get_channel_lock(channel_id)
    async with lock:
        result, timed_out = await run_claude(meta_prompt)

    if timed_out:
        return {"use_all": True, "keywords": [], "date_from": None, "date_to": None}

    try:
        m = re.search(r"\{.*\}", result, re.DOTALL)
        if m:
            return json.loads(m.group())
    except (json.JSONDecodeError, ValueError):
        pass

    return {"use_all": True, "keywords": [], "date_from": None, "date_to": None}


def register(bot: "SlackBot"):
    """要約コマンドを Slack app に登録する。"""
    app = bot.app

    @app.command("/summarize-ai")
    async def cmd_summarize(ack, respond, command, client):
        await ack()
        channel_id = command.get("channel_id", "")
        channel_name = command.get("channel_name", channel_id)
        user_id = command.get("user_id", "")
        question_text = (command.get("text") or "").strip()
        question = question_text or "主なトピック・決定事項・重要な発言を簡潔に日本語でまとめてください。"

        await respond(text=":hourglass: メッセージを取得中...", response_type="ephemeral")
        logger.info("summarize: start ch=%s prompt=%r", channel_id, question)

        tmp_path: Path | None = None
        try:
            # ─── メッセージを収集して tmp ファイルに書き出す ──
            tmp_fd, tmp_str = tempfile.mkstemp(
                prefix=f"summarize_{channel_id}_",
                suffix=".txt",
                dir=_cfg.TMP_DIR,
            )
            tmp_path = Path(tmp_str)

            total_chars = 0
            truncated = False
            msg_count = 0
            msg_cursor = None

            with open(tmp_fd, "w", encoding="utf-8") as f:
                while True:
                    try:
                        kwargs: dict = {
                            "channel": channel_id,
                            "limit": 200,
                            "inclusive": True,
                        }
                        if msg_cursor:
                            kwargs["cursor"] = msg_cursor
                        resp = await client.conversations_history(**kwargs)
                        messages = resp.get("messages", [])
                        for msg in reversed(messages):  # 古い順
                            if msg.get("subtype") or not msg.get("text"):
                                continue
                            ts_float = float(msg["ts"])
                            dt = datetime.fromtimestamp(ts_float, tz=JST)
                            ts_str = dt.strftime("%Y-%m-%d %H:%M")
                            raw_user = msg.get("username") or msg.get("user", "unknown")
                            user = await _resolve_user(client, raw_user)
                            line = f"[{ts_str}] {user}: {msg['text']}\n"
                            f.write(line)
                            total_chars += len(line)
                            msg_count += 1
                            if total_chars >= FETCH_CHAR_CAP:
                                truncated = True
                                break
                        if truncated:
                            break
                        next_cursor = resp.get("response_metadata", {}).get("next_cursor", "")
                        if not next_cursor or not resp.get("has_more", False):
                            break
                        msg_cursor = next_cursor
                    except Exception as e:
                        logger.warning("summarize: history fetch error: %s", e)
                        break

            logger.info("summarize: fetched %d msgs, truncated=%s", msg_count, truncated)
            suffix = "（収集上限到達）" if truncated else ""

            if msg_count == 0:
                await client.chat_postMessage(
                    channel=user_id,
                    text=":information_source: メッセージが見つかりませんでした。",
                )
                return

            await client.chat_postMessage(
                channel=user_id,
                text=f":speech_balloon: {msg_count}件のメッセージを取得しました{suffix}。分析中...",
            )

            # ─── Stage 1: 検索条件を抽出 ──────────────────────
            raw = tmp_path.read_text(encoding="utf-8")
            head = raw[:SAMPLE_HEAD_CHARS]
            tail = raw[-SAMPLE_TAIL_CHARS:] if len(raw) > SAMPLE_HEAD_CHARS + SAMPLE_TAIL_CHARS else ""
            sample = head + ("\n...\n" + tail if tail else "")

            criteria = await _get_search_criteria(bot, question, sample, channel_id)

            # ─── フィルタリング ────────────────────────────────
            use_all = criteria.get("use_all", True)
            keywords = [kw.lower() for kw in criteria.get("keywords", []) if kw] if not use_all else []
            date_from = date_to = None

            if not use_all:
                if criteria.get("date_from"):
                    try:
                        date_from = datetime.strptime(criteria["date_from"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                if criteria.get("date_to"):
                    try:
                        date_to = datetime.strptime(criteria["date_to"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
                    except ValueError:
                        pass

            lines = []
            char_count = 0
            with open(tmp_path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue

                    if date_from or date_to:
                        m = re.match(r"\[(\d{4}-\d{2}-\d{2})", line)
                        if m:
                            try:
                                line_dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                                if date_from and line_dt < date_from:
                                    continue
                                if date_to and line_dt >= date_to:
                                    continue
                            except ValueError:
                                pass

                    if keywords and not any(kw in line.lower() for kw in keywords):
                        continue

                    if char_count + len(line) + 1 > CHAR_LIMIT:
                        break
                    lines.append(line)
                    char_count += len(line) + 1

            if not lines:
                with open(tmp_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        if char_count + len(line) + 1 > CHAR_LIMIT:
                            break
                        lines.append(line)
                        char_count += len(line) + 1

            # ─── Stage 2: 最終回答を生成 ──────────────────────
            history_text = "\n".join(lines)
            full_prompt = (
                f"以下は #{channel_name} チャンネルの Slack 会話ログ（{len(lines)}件 / 全{msg_count}件）です。\n"
                f"{question}\n\n"
                + history_text
            )

            from platforms.slack import SLACK_FORMAT_HINT
            skill_instr = f"[platform: slack]\n\n{SLACK_FORMAT_HINT}"

            lock = bot.get_channel_lock(channel_id)
            async with lock:
                summary, timed_out = await run_claude(full_prompt, skill_instructions=skill_instr)

            if timed_out:
                await client.chat_postMessage(
                    channel=user_id,
                    text=":warning: タイムアウトしました。",
                )
                return

            display_summary = re.sub(r"\n{3,}", "\n\n", summary) if summary else ""
            chunks = split_message(display_summary, max_len=3000)
            for chunk in chunks:
                await client.chat_postMessage(channel=channel_id, text=chunk)

        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
