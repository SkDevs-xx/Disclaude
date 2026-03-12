"""
Skill dataclass definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    """SKILL.md から読み込まれたスキル定義。"""
    name: str
    description: str
    instructions: str                              # Markdown 本文（LLM に注入する指示）
    source_path: Path                              # SKILL.md のパス
    platforms: frozenset[str] = field(             # 対応プラットフォーム（空 = 全対応）
        default_factory=frozenset,
    )
    user_invocable: bool = False                   # ユーザーが直接呼べるか
    slow: bool = False                             # 処理に時間がかかるか（事前通知用）
    slow_keywords: frozenset[str] = field(         # slow通知のトリガーキーワード
        default_factory=frozenset,
    )
