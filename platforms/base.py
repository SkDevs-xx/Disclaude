"""
プラットフォーム抽象化の基底クラスとコンテキスト
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PlatformContext:
    """プラットフォーム固有の実行コンテキスト。"""
    name: str                                   # "discord" | "slack" | "notion"
    workspace_dir: Path                         # platforms/{name}/workspace/
    capabilities: frozenset[str] = field(       # {"embed", "reaction", "thread", ...}
        default_factory=frozenset,
    )
    format_hint: str = ""                       # 出力形式ヒント（Discordマークダウン制約等）
    disabled_skills: frozenset[str] = field(    # このプラットフォームで無効なスキル名
        default_factory=frozenset,
    )
