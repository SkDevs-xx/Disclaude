"""
Slack /review-ai ハンドラ
調査済みトピックのフィードバックレビュー
Discord ReviewCog と同等のロジック
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING

import core.config as _cfg
from core.config import get_channel_session, save_channel_session, get_model_config
from core.engine import run_engine
from core.message import split_message
from core.memory import parse_pending_reviews, resolve_archive

if TYPE_CHECKING:
    from platforms.slack.bot import SlackBot

logger = logging.getLogger("slack_bot")

MAX_REVIEW_ITEMS = 3


def _review_file():
    return _cfg.WORKFLOW_DIR / "REVIEW.md"


def register(bot: "SlackBot"):
    """レビューコマンドを Slack app に登録する。"""
    app = bot.app

    @app.command("/review-ai")
    async def cmd_review(ack, respond, command, client):
        await ack()

        if not _review_file().exists():
            await respond(text=":information_source: REVIEW.md が見つかりません。", response_type="ephemeral")
            return

        import asyncio
        text = await asyncio.to_thread(_review_file().read_text, encoding="utf-8")
        items = parse_pending_reviews(text)

        if not items:
            await respond(text=":white_check_mark: レビュー待ちの項目はないよ。", response_type="ephemeral")
            return

        selected = items[:MAX_REVIEW_ITEMS]

        sections: list[str] = []
        for i, item in enumerate(selected, 1):
            header = f"## {i}. {item['topic']}"
            content = ""
            if item["archive_rel"]:
                path = resolve_archive(_cfg.WORKFLOW_DIR, item["archive_rel"])
                if path:
                    import asyncio
                    content = await asyncio.to_thread(path.read_text, encoding="utf-8")
                else:
                    content = "（アーカイブファイルが見つかりませんでした）"
            else:
                content = "（アーカイブパスが記載されていません）"
            sections.append(f"{header}\n（アーカイブ: {item['archive_rel'] or '不明'}）\n\n{content}")

        body = "\n\n---\n\n".join(sections)
        prompt = (
            "以下はこれまでの調査結果です。各トピックの要点をわかりやすく要約して提示し、"
            "ユーザに感想や意見を聞いてください。\n\n"
            "【重要】ユーザからフィードバックを受けたときの応答ルール:\n"
            "1. 必ず最初に、各フィードバックに対するあなた自身の感想・考え・気づきを述べること（これが最優先）\n"
            "2. 共感、新たな視点、関連する知見など、会話として自然に反応すること\n"
            "3. 感想を述べた後で、アーカイブへの記録とREVIEW.mdの更新を行うこと\n"
            "4. 記録作業は裏で行い、ユーザへの応答では感想と対話を中心にすること\n\n"
            "記録手順: 該当アーカイブに「## ユーザフィードバック」として記録し、"
            "REVIEW.md の該当行を「未レビュー」から「フィードバック済み」セクションに移動して [x] に変更する。\n\n"
            f"---\n\n{body}"
        )

        # 実行中メッセージ
        await respond(text=":hourglass: レビュー内容を準備中...", response_type="in_channel")

        channel_id = command.get("channel_id", "")
        session_id = get_channel_session(channel_id)
        is_new = session_id is None

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
        response_text, timed_out, new_session_id = await run_engine(
            prompt,
            model=model,
            thinking=thinking,
            session_id=session_id,
            is_new_session=is_new,
            skill_instructions=skill_instr,
        )
        
        if is_new and new_session_id:
            save_channel_session(channel_id, new_session_id)

        if timed_out:
            await client.chat_postMessage(
                channel=channel_id,
                text=":warning: タイムアウトしました。もう一度お試しください。",
            )
            return

        display = re.sub(r"\n{3,}", "\n\n", response_text)
        chunks = split_message(display, max_len=3000)
        for chunk in chunks:
            await client.chat_postMessage(channel=channel_id, text=chunk)
