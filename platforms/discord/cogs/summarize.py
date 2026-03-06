"""
SummarizeCog: /summarize
Discord API でチャンネルの全メッセージを取得し、2段階 Claude 呼び出しで要約・質問に回答する

処理フロー:
  1. Discord から全メッセージを収集し /tmp/ にテキストファイルを書き出す
  2. Stage 1: ファイルの先頭・末尾サンプル + プロンプト → Claude が検索条件 (JSON) を返す
  3. Python: ファイル全体をキーワード/日付でフィルタリング
  4. Stage 2: 絞り込んだ行を Claude に渡して最終回答を生成
  5. finally: tmp ファイルを必ず削除
"""

import json
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.engine import run_engine
import core.config as _cfg
from core.message import split_message
from platforms.discord.embeds import make_error_embed, make_info_embed

logger = logging.getLogger("discord_bot")

CHAR_LIMIT = 600_000       # Stage 2 で Claude に渡す最大文字数
FETCH_CHAR_CAP = 2_000_000 # Discord 収集フェーズの文字数上限（無限ループ防止）
SAMPLE_HEAD_CHARS = 3_000  # Stage 1 に渡す先頭サンプル文字数
SAMPLE_TAIL_CHARS = 3_000  # Stage 1 に渡す末尾サンプル文字数


class SummarizeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_search_criteria(
        self, question: str, sample_text: str, channel_id: int
    ) -> dict:
        """Stage 1: メッセージサンプル + プロンプトから検索条件を Claude で抽出する"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        meta_prompt = (
            f"以下はDiscordチャンネルの会話ログのサンプルです。\n\n"
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

        async with self.bot.get_channel_lock(channel_id):
            result, timed_out = await run_engine(meta_prompt)

        if timed_out:
            return {"use_all": True, "keywords": [], "date_from": None, "date_to": None}

        try:
            m = re.search(r"\{.*\}", result, re.DOTALL)
            if m:
                return json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            pass

        return {"use_all": True, "keywords": [], "date_from": None, "date_to": None}

    @app_commands.command(
        name="summarize",
        description="このチャンネルの会話をClaudeに質問・要約する",
    )
    @app_commands.describe(
        prompt="Claudeへの質問・指示（例: 先週の話題をまとめて / 株価について教えて / 省略時はデフォルト要約）",
    )
    async def summarize_command(
        self,
        interaction: discord.Interaction,
        prompt: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        logger.info("summarize: start ch=%d prompt=%r", interaction.channel_id, prompt)

        # スレッドにも対応: get_channel_or_thread でキャッシュ済みオブジェクトを取得
        channel = self.bot.get_channel(interaction.channel_id) or interaction.channel
        if channel is None:
            await interaction.followup.send(
                embed=make_error_embed("チャンネルが見つかりません。"), ephemeral=True
            )
            return

        question = prompt or "主なトピック・決定事項・重要な発言を簡潔に日本語でまとめてください。"

        tmp_path: Path | None = None
        try:
            # ─── Discord から全メッセージを収集して tmp ファイルに書き出す ────────
            tmp_fd, tmp_str = tempfile.mkstemp(
                prefix=f"summarize_{interaction.channel_id}_",
                suffix=".txt",
                dir=_cfg.TMP_DIR,
            )
            tmp_path = Path(tmp_str)

            total_chars = 0
            truncated = False
            msg_count = 0

            try:
                with open(tmp_fd, "w", encoding="utf-8") as f:
                    async for msg in channel.history(limit=None, oldest_first=True):
                        if not msg.content:
                            continue
                        ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
                        line = f"[{ts}] {msg.author.display_name}: {msg.content}\n"
                        f.write(line)
                        total_chars += len(line)
                        msg_count += 1
                        if total_chars >= FETCH_CHAR_CAP:
                            truncated = True
                            break
            except discord.Forbidden:
                await interaction.followup.send(
                    embed=make_error_embed(
                        "メッセージ履歴の読み取り権限がありません。\n"
                        "Discordのチャンネル権限で「メッセージ履歴を読む」をボットに付与してください。"
                    ),
                    ephemeral=True,
                )
                return
            except Exception as e:
                logger.exception("summarize: history fetch error: %s", e)
                await interaction.followup.send(
                    embed=make_error_embed(f"メッセージ取得中にエラーが発生しました: {e}"),
                    ephemeral=True,
                )
                return

            logger.info("summarize: fetched %d msgs, truncated=%s", msg_count, truncated)
            suffix = "（収集上限到達）" if truncated else ""
            await interaction.edit_original_response(
                content=f"💬 {msg_count}件のメッセージを取得しました{suffix}。分析中..."
            )

            if msg_count == 0:
                await interaction.followup.send(
                    embed=make_info_embed("要約", "メッセージが見つかりませんでした。"),
                    ephemeral=True,
                )
                return

            # ─── tmp ファイルから先頭・末尾サンプルを生成 ──────────────────────
            with open(tmp_path, "rb") as _f:
                head_bytes = _f.read(SAMPLE_HEAD_CHARS * 4)
                _f.seek(0, 2)
                _total = _f.tell()
                if _total > (SAMPLE_HEAD_CHARS + SAMPLE_TAIL_CHARS) * 4:
                    _f.seek(max(0, _total - SAMPLE_TAIL_CHARS * 4))
                    tail_bytes = _f.read()
                else:
                    tail_bytes = b""
            head = head_bytes.decode("utf-8", errors="replace")[:SAMPLE_HEAD_CHARS]
            tail = tail_bytes.decode("utf-8", errors="replace")[-SAMPLE_TAIL_CHARS:] if tail_bytes else ""
            sample = head + ("\n...\n" + tail if tail else "")

            # ─── Stage 1: 検索条件を Claude で抽出 ───────────────────────────────
            criteria = await self._get_search_criteria(question, sample, interaction.channel_id)

            # ─── Python: tmp ファイルをフィルタリング ────────────────────────────
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

                    # 日付フィルタ: 行頭の "[YYYY-MM-DD" から判定
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

                    # キーワードフィルタ
                    if keywords and not any(kw in line.lower() for kw in keywords):
                        continue

                    if char_count + len(line) + 1 > CHAR_LIMIT:
                        break
                    lines.append(line)
                    char_count += len(line) + 1

            # フィルタ結果が空なら全件（上限まで）にフォールバック
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

            # ─── Stage 2: 最終回答を生成 ─────────────────────────────────────────
            await interaction.edit_original_response(content="📝 まとめを生成中...")
            history_text = "\n".join(lines)
            full_prompt = (
                f"以下は #{channel.name} チャンネルのDiscord会話ログ（{len(lines)}件 / 全{msg_count}件）です。\n"
                f"{question}\n\n"
                + history_text
            )

            async with self.bot.get_channel_lock(interaction.channel_id):
                summary, timed_out = await run_engine(full_prompt)

            if timed_out:
                await interaction.followup.send(
                    embed=make_error_embed("タイムアウトしました。"), ephemeral=True
                )
                return

            display_summary = re.sub(r'\n{2,}', '\n', summary) if summary else ""
            chunks = split_message(display_summary, max_len=2000)
            for chunk in chunks:
                await interaction.followup.send(content=chunk)

        finally:
            # tmp ファイルを必ず削除
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()


async def setup(bot: commands.Bot):
    await bot.add_cog(SummarizeCog(bot))
