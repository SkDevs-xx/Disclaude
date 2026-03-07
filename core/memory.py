"""
workspace のマークダウンファイル (HEARTBEAT.md, REVIEW.md 等) のパース・書き込み
プラットフォーム非依存 — ファイルパスは呼び出し側から受け取る
"""

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


# ─────────────────────────────────────────────
# HEARTBEAT.md
# ─────────────────────────────────────────────

def parse_heartbeat_state(text: str) -> dict:
    """## State セクションの key-value をパースする。"""
    state = {
        "last_updated": None,
        "wrapup_done": False,
        "wrapup_time": "05:00",
        "last_wrapup_compressed": None,
        "last_weekly_compressed": None,
    }
    m = re.search(r"wrapup_done:\s*(true|false)", text, re.IGNORECASE)
    if m:
        state["wrapup_done"] = m.group(1).lower() == "true"
    m = re.search(r'wrapup_time:\s*["\']?(\d{2}:\d{2})["\']?', text)
    if m:
        state["wrapup_time"] = m.group(1)
    m = re.search(r"last_updated:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        state["last_updated"] = m.group(1)
    m = re.search(r"last_wrapup_compressed:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        state["last_wrapup_compressed"] = m.group(1)
    m = re.search(r"last_weekly_compressed:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        state["last_weekly_compressed"] = m.group(1)
    return state


async def update_heartbeat_state(heartbeat_path: Path, key: str, value: str) -> None:
    """HEARTBEAT.md 内の指定キーの値を書き換える。"""
    await update_heartbeat_states(heartbeat_path, {key: value})


async def update_heartbeat_states(heartbeat_path: Path, updates: dict[str, str]) -> None:
    """HEARTBEAT.md 内の複数キーの値を一括で書き換える（アトミック）。"""
    import asyncio
    import os
    import tempfile
    if not heartbeat_path.exists() or not updates:
        return

    def _atomic_update():
        text = heartbeat_path.read_text(encoding="utf-8")
        for key, value in updates.items():
            pattern = rf"({re.escape(key)}:\s*).*"
            text = re.sub(pattern, rf"\g<1>{value}", text)
        fd, tmp = tempfile.mkstemp(dir=heartbeat_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, heartbeat_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    await asyncio.to_thread(_atomic_update)


def get_checklist_section(text: str) -> str:
    """「## 毎回チェック」セクションの内容を抽出する。"""
    m = re.search(r"## 毎回チェック\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


async def update_checklist_section(heartbeat_path: Path, new_content: str) -> None:
    """HEARTBEAT.md の「## 毎回チェック」セクションを書き換える。"""
    import asyncio
    import os
    import tempfile
    if not heartbeat_path.exists():
        return

    def _atomic_update():
        text = heartbeat_path.read_text(encoding="utf-8")
        new_text = re.sub(
            r"(## 毎回チェック\n).*?(?=\n## |\Z)",
            rf"\g<1>{new_content}\n",
            text,
            flags=re.DOTALL,
        )
        fd, tmp = tempfile.mkstemp(dir=heartbeat_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, heartbeat_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    await asyncio.to_thread(_atomic_update)


def should_run_wrapup(state: dict) -> bool:
    """wrapup_done == false かつ現在時刻 >= wrapup_time なら True。"""
    if state.get("wrapup_done"):
        return False
    wrapup_time_str = state.get("wrapup_time", "05:00")
    now = datetime.now(JST)
    try:
        h, m = wrapup_time_str.split(":")
        wrapup_dt = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        return now >= wrapup_dt
    except (ValueError, AttributeError):
        return False


# ─────────────────────────────────────────────
# REVIEW.md
# ─────────────────────────────────────────────

_ARCHIVE_PATH_RE = re.compile(r"`(memory/curiosity/[\w./-]+\.md)`")


def parse_pending_reviews(text: str) -> list[dict]:
    """REVIEW.md から未レビュー（[ ]）行を解析する。"""
    items: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- [ ]"):
            continue
        topic_part = stripped.removeprefix("- [ ]").strip()
        topic = re.split(r"\s*→\s*`", topic_part)[0].strip()
        path_m = _ARCHIVE_PATH_RE.search(stripped)
        archive_rel = path_m.group(1) if path_m else None
        items.append({"topic": topic, "archive_rel": archive_rel, "line": stripped})
    return items


def resolve_archive(workflow_dir: Path, relative: str) -> Path | None:
    """REVIEW.md 記載の相対パスを実ファイルに解決する。"""
    full = workflow_dir / relative
    if full.exists():
        return full
    curiosity_dir = workflow_dir / "memory" / "curiosity"
    fname = full.name
    for sub in ("self", "tech", "business"):
        candidate = curiosity_dir / sub / fname
        if candidate.exists():
            return candidate
    return None
