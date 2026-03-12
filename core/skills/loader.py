"""
SKILL.md parser: YAML frontmatter + Markdown body.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from core.skills.models import Skill

logger = logging.getLogger("discord_bot")

_FRONTMATTER_DELIMITER = "---"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """YAML frontmatter と Markdown 本文を分離する。

    Returns:
        (frontmatter_yaml, body_markdown)
    """
    stripped = text.lstrip("\n")
    if not stripped.startswith(_FRONTMATTER_DELIMITER):
        return "", text

    # 2 番目の --- を探す
    end = stripped.find(_FRONTMATTER_DELIMITER, len(_FRONTMATTER_DELIMITER))
    if end == -1:
        return "", text

    fm = stripped[len(_FRONTMATTER_DELIMITER):end].strip()
    body = stripped[end + len(_FRONTMATTER_DELIMITER):].strip()
    return fm, body


def load_skill(skill_md_path: Path) -> tuple[Skill | None, str | None]:
    """SKILL.md ファイルを読み込んで Skill オブジェクトとエラーメッセージのタプルを返す。
    エラーがなければ (Skill, None)、失敗時は (None, error_msg) を返す。
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        err = f"Failed to read file: {e}"
        logger.warning("Failed to read skill file %s: %s", skill_md_path, e)
        return None, err

    fm_text, body = _split_frontmatter(text)
    if not fm_text:
        err = "No frontmatter found (missing --- block)"
        logger.warning("No frontmatter found in %s", skill_md_path)
        return None, err

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        err = f"Invalid YAML: {e}"
        logger.warning("Invalid YAML in %s: %s", skill_md_path, e)
        return None, err

    if not isinstance(meta, dict):
        err = "Frontmatter is not a YAML dictionary mapping"
        logger.warning("Frontmatter is not a mapping in %s", skill_md_path)
        return None, err

    name = meta.get("name")
    if not name:
        err = "Missing 'name' in frontmatter"
        logger.warning("Missing 'name' in frontmatter of %s", skill_md_path)
        return None, err

    platforms_raw = meta.get("platforms", [])
    platforms = frozenset(str(p) for p in platforms_raw) if platforms_raw else frozenset()

    skill = Skill(
        name=str(name),
        description=str(meta.get("description", "")),
        instructions=body,
        source_path=skill_md_path,
        platforms=platforms,
        user_invocable=bool(meta.get("user-invocable", False)),
        slow=bool(meta.get("slow", False)),
        slow_keywords=frozenset(str(k) for k in meta.get("slow-keywords", [])),
    )
    return skill, None
