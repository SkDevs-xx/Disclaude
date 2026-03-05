"""
Claude Code CLI 実行
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from core.config import (
    CLAUDE_BIN,
    BASE_DIR,
    TIMEOUT_FAST, TIMEOUT_PLANNING,
    get_skip_permissions,
)

def _logger() -> logging.Logger:
    from core.config import _tl_get
    name = _tl_get("PLATFORM_NAME")
    return logging.getLogger(f"{name}_bot" if name else "discord_bot")


async def run_claude(
    prompt: str,
    model: str = "sonnet",
    thinking: bool = False,
    timeout: int | None = None,
    session_id: str | None = None,
    is_new_session: bool = False,
    on_process: "Callable[[asyncio.subprocess.Process], None] | None" = None,
    skill_instructions: str = "",
) -> tuple[str, bool]:
    """(response_text, timed_out) を返す。

    on_process: プロセス起動直後に呼ばれるコールバック。
    外部からプロセスを参照してキャンセルする用途に使う。
    skill_instructions: スキルエンジンから注入される追加指示。
    プロンプトの先頭に付加される。
    """
    if timeout is None:
        timeout = TIMEOUT_PLANNING if thinking else TIMEOUT_FAST

    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "text"]
    if skill_instructions:
        cmd += ["--system-prompt", skill_instructions]
    if get_skip_permissions():
        cmd.append("--dangerously-skip-permissions")
    cmd += ["--model", model]
    if thinking:
        cmd += ["--effort", "high"]
    if session_id:
        if is_new_session:
            cmd += ["--session-id", session_id]
        else:
            cmd += ["--resume", session_id]

    total_len = len(prompt) + (len(skill_instructions) if skill_instructions else 0)
    _logger().info("claude: model=%s thinking=%s prompt_len=%d timeout=%ds", model, thinking, total_len, timeout)

    env = dict(os.environ)
    if skill_instructions:
        m = re.search(r"\[platform:\s*(\w+)\]", skill_instructions)
        if m:
            env["DISCLAUDE_PLATFORM"] = m.group(1)

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            env=env,
        )
        if on_process is not None:
            on_process(proc)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            _logger().error("claude error (rc=%d): %s", proc.returncode, err)
            err_lower = err.lower()
            if any(kw in err_lower for kw in ("usage limit", "rate limit", "quota", "plan limit", "exceeded")):
                return "現在、プランの使用制限に達しているため利用できません。しばらく時間をおいてから再度お試しください。", False
            return f"エラーが発生しました（終了コード {proc.returncode}）:\n```\n{err[:800]}\n```", False
        return stdout.decode("utf-8", errors="replace").strip(), False
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return "", True
    except asyncio.CancelledError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        raise  # タスクキャンセルは上位に伝搬させる
