"""
ReviewCog: /review — 調査済みトピックのフィードバックレビュー
"""

from __future__ import annotations

import re
import uuid

import discord
from discord import app_commands
from discord.ext import commands

import core.config as _cfg
from core.config import (
    get_channel_session,
    save_channel_session,
)
from core.engine import run_engine
from platforms.discord.embeds import make_info_embed, make_error_embed
from core.message import split_message
from core.memory import parse_pending_reviews, resolve_archive


def _review_file():
    """init_workspace() 後の WORKFLOW_DIR を反映して REVIEW.md のパスを返す。"""
    return _cfg.WORKFLOW_DIR / "REVIEW.md"
MAX_REVIEW_ITEMS = 3


class ReviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="review", description="調査済みトピックをレビューする")
    async def review_command(self, interaction: discord.Interaction):
        if not _review_file().exists():
            await interaction.response.send_message(
                embed=make_info_embed("レビュー", "REVIEW.md が見つかりません。"),
                ephemeral=True,
            )
            return

        import asyncio
        text = await asyncio.to_thread(_review_file().read_text, encoding="utf-8")
        items = parse_pending_reviews(text)

        if not items:
            await interaction.response.send_message(
                embed=make_info_embed("レビュー", "レビュー待ちの項目はないよ。"),
                ephemeral=True,
            )
            return

        selected = items[:MAX_REVIEW_ITEMS]

        # アーカイブ内容を読み込む
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

        await interaction.response.defer()

        channel_id = interaction.channel_id
        session_id = get_channel_session(channel_id)
        is_new = session_id is None

        from core.config import get_model_config
        model, thinking = get_model_config()
        response, timed_out, new_session_id = await run_engine(
            prompt,
            model=model,
            thinking=thinking,
            session_id=session_id,
            is_new_session=is_new,
        )
        if is_new and new_session_id:
            save_channel_session(channel_id, new_session_id)

        if timed_out:
            await interaction.followup.send(
                embed=make_error_embed("タイムアウトしました。もう一度お試しください。")
            )
            return

        display = re.sub(r"\n{2,}", "\n", response)
        chunks = split_message(display, max_len=2000)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReviewCog(bot))
