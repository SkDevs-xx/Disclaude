"""
Skill registry: scan directories, register skills, match by name/platform.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.skills.loader import load_skill
from core.skills.models import Skill

logger = logging.getLogger("discord_bot")


class SkillRegistry:
    """スキルの登録・検索を管理する。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self.load_errors: list[tuple[Path, str]] = []

    def register(self, skill: Skill) -> None:
        """スキルを登録する。同名のスキルは上書き。"""
        self._skills[skill.name] = skill
        logger.debug("Skill registered: %s", skill.name)

    def reload(self, skills_dir: Path) -> int:
        """スキルを再スキャンして登録する（アトミック更新）。"""
        return self.scan_directory(skills_dir)

    def scan_directory(self, skills_dir: Path) -> int:
        """ディレクトリ配下の SKILL.md を再帰スキャンして登録する（アトミック更新）。

        Returns:
            登録されたスキル数
        """
        count = 0
        if not skills_dir.is_dir():
            logger.warning("Skills directory not found: %s", skills_dir)
            return 0

        new_skills: dict[str, Skill] = {}
        new_errors: list[tuple[Path, str]] = []

        for skill_file in sorted(skills_dir.rglob("SKILL.md")):
            skill, err = load_skill(skill_file)
            if skill is not None:
                new_skills[skill.name] = skill
                count += 1
            elif err is not None:
                new_errors.append((skill_file, err))

        # アトミックに参照を入れ替え
        self._skills = new_skills
        self.load_errors = new_errors

        logger.info("Scanned %s: %d skill(s) loaded atomically", skills_dir, count)
        return count

    def get(self, name: str) -> Skill | None:
        """名前でスキルを取得する。"""
        return self._skills.get(name)

    def get_for_platform(
        self,
        platform: str,
        *,
        disabled: frozenset[str] = frozenset(),
    ) -> list[Skill]:
        """指定プラットフォームで利用可能なスキルを返す。

        Args:
            platform: プラットフォーム名 ("discord", "slack", ...)
            disabled: 無効にするスキル名の集合
        """
        result = []
        for skill in self._skills.values():
            if skill.name in disabled:
                continue
            # platforms が空 = 全プラットフォーム対応
            if skill.platforms and platform not in skill.platforms:
                continue
            result.append(skill)
        return result

    def all_skills(self) -> list[Skill]:
        """登録済みの全スキルを返す。"""
        return list(self._skills.values())

    def build_instructions(
        self,
        platform: str,
        *,
        disabled: frozenset[str] = frozenset(),
    ) -> str:
        """プラットフォーム向けのスキル指示テキストを結合して返す。

        空文字列を返す場合、注入すべきスキルがないことを意味する。
        """
        skills = self.get_for_platform(platform, disabled=disabled)
        if not skills:
            return ""

        parts = []
        for skill in skills:
            if skill.instructions:
                parts.append(
                    f"## Skill: {skill.name}\n"
                    f"{skill.instructions}"
                )
        return "\n\n".join(parts)
