"""
ラップアップ・メモリ圧縮
プラットフォーム非依存 — メッセージ収集は callable で受け取る
"""

import logging
from collections.abc import Callable, Awaitable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import core.config as _cfg

JST = ZoneInfo("Asia/Tokyo")


def _logger() -> logging.Logger:
    return _cfg._logger()


def get_wrapup_dir() -> Path:
    """MEMORY_DIR の現在値から wrapup ディレクトリを解決する。"""
    return _cfg.MEMORY_DIR / "wrapup"


def daily_wrapup_path(guild_id: int, target_date) -> Path:
    """新パス: memory/wrapup/{guild_id}/YYYY-MM-DD.md"""
    return get_wrapup_dir() / str(guild_id) / f"{target_date.strftime('%Y-%m-%d')}.md"


WRAPUP_CHAR_CAP = 800_000  # 収集フェーズの文字数上限


class CollectedMessages(NamedTuple):
    """メッセージ収集の結果。"""
    parts: dict[str, list[str]]  # channel_name -> lines
    total_chars: int
    total_msgs: int
    truncated: bool


# collector の型: async (after_dt, before_dt) -> CollectedMessages
MessageCollector = Callable[
    [datetime, datetime],
    Awaitable[CollectedMessages],
]


async def run_wrapup(
    guild_id: int,
    guild_name: str,
    collect_messages: MessageCollector,
    format_hint: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    wrapup_time: str = "00:00",
) -> str | None:
    """
    メッセージを収集して Claude で要約する。
    collect_messages: プラットフォーム固有のメッセージ収集関数
    format_hint: 出力形式の指示（プラットフォーム固有）
    """
    from core.engine import run_engine

    # ── wrapup_time のパース ──
    wt_h, wt_m = (int(x) for x in wrapup_time.split(":"))

    # ── 日付範囲の決定 ──
    now = datetime.now(JST)
    if date_from is None and date_to is None:
        end_dt = now.replace(hour=wt_h, minute=wt_m, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=1)
        d_from = start_dt.date()
        d_to = end_dt.date()
    else:
        today = now.date()
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else today - timedelta(days=1)
        d_to = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else today
        start_dt = datetime(d_from.year, d_from.month, d_from.day, wt_h, wt_m, tzinfo=JST)
        end_dt = datetime(d_to.year, d_to.month, d_to.day, wt_h, wt_m, tzinfo=JST)

    # Discord API 用（after は排他なので1秒引く）
    after_dt = start_dt - timedelta(seconds=1)
    before_dt = end_dt

    # ── メッセージ収集（プラットフォーム固有の collector を呼ぶ） ──
    result = await collect_messages(after_dt, before_dt)

    if not result.parts:
        return None

    # ── チャンネル別にテキストを組み立て ──
    history_lines = []
    for ch_name, lines in result.parts.items():
        history_lines.append(f"### #{ch_name}")
        history_lines.extend(lines)
        history_lines.append("")
    history_text = "\n".join(history_lines)

    # ── 期間ラベル ──
    date_label = d_from.strftime("%Y-%m-%d")
    if d_from != d_to:
        date_label += f" 〜 {d_to.strftime('%Y-%m-%d')}"

    _logger().info("wrapup: guild=%d period=%s msgs=%d chars=%d truncated=%s",
                guild_id, date_label, result.total_msgs, result.total_chars, result.truncated)

    # ── Claude に要約を依頼 ──
    prompt = (
        f"{date_label} のサーバー「{guild_name}」全チャンネルの会話ログ（{result.total_msgs}件）です。\n"
        "この期間に話したこと・決めたこと・進んだこと・残ったタスクをチャンネルをまたいで簡潔にまとめてください。\n"
    )
    if format_hint:
        prompt += "\n" + format_hint + "\n"
    prompt += "\n" + history_text

    summary, timed_out = await run_engine(prompt)

    if timed_out or not summary or summary.startswith("エラーが発生しました"):
        return None

    # ── 保存 ──
    guild_dir = get_wrapup_dir() / str(guild_id)
    guild_dir.mkdir(parents=True, exist_ok=True)
    wp_file = daily_wrapup_path(guild_id, d_from)
    
    import asyncio
    import tempfile
    import os

    def _safe_write():
        fd, tmp = tempfile.mkstemp(dir=guild_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"# {date_label}\n\n{summary}\n")
            os.replace(tmp, wp_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    await asyncio.to_thread(_safe_write)

    return summary
