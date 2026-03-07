"""
LLM エンジン実行
config.json の "engine" フィールドに応じて Claude Code CLI / Codex CLI などを切り替える。

対応エンジン:
  "claude" (デフォルト) — Claude Code CLI
  "codex"               — OpenAI Codex CLI（将来実装）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import core.config as _cfg
from core.config import BASE_DIR, TIMEOUT_FAST, TIMEOUT_PLANNING, get_skip_permissions


def _logger() -> logging.Logger:
    return _cfg._logger()


async def run_engine(
    prompt: str,
    model: str = "sonnet",
    thinking: bool = False,
    timeout: int | None = None,
    session_id: str | None = None,
    is_new_session: bool = False,
    on_process: "Callable[[asyncio.subprocess.Process], None] | None" = None,
    skill_instructions: str = "",
) -> tuple[str, bool, str | None]:
    """(response_text, timed_out, new_session_id) を返す。

    config.json の "engine" に応じて実装を切り替える。
    on_process: プロセス起動直後に呼ばれるコールバック。キャンセル用途。
    skill_instructions: プロンプト先頭に付加されるスキル追加指示。
    """
    engine = _cfg.get_engine_name()
    if engine == "codex":
        return await _run_codex_cli(
            prompt, model, thinking, timeout, session_id, is_new_session, on_process, skill_instructions
        )
    return await _run_claude_cli(
        prompt, model, thinking, timeout,
        session_id, is_new_session, on_process, skill_instructions,
    )


async def _run_claude_cli(
    prompt: str,
    model: str,
    thinking: bool,
    timeout: int | None,
    session_id: str | None,
    is_new_session: bool,
    on_process: "Callable[[asyncio.subprocess.Process], None] | None",
    skill_instructions: str,
) -> tuple[str, bool, str | None]:
    """Claude Code CLI を subprocess で実行する。"""
    from core.config import DEFAULT_ENGINE_BIN

    if timeout is None:
        timeout = TIMEOUT_PLANNING if thinking else TIMEOUT_FAST

    cmd = [DEFAULT_ENGINE_BIN, "-p", "--output-format", "text"]
    if skill_instructions:
        cmd += ["--system-prompt", skill_instructions]
    if get_skip_permissions():
        cmd.append("--dangerously-skip-permissions")
    cmd += ["--model", model]
    cmd += ["--settings", json.dumps({"alwaysThinkingEnabled": thinking})]
    
    new_session_id = None
    if is_new_session and not session_id:
        session_id = str(uuid.uuid4())
        new_session_id = session_id
        
    if session_id:
        if is_new_session:
            cmd += ["--session-id", session_id]
        else:
            cmd += ["--resume", session_id]

    total_len = len(prompt) + (len(skill_instructions) if skill_instructions else 0)
    _logger().info(
        "engine=claude model=%s thinking=%s prompt_len=%d timeout=%ds",
        model, thinking, total_len, timeout,
    )

    env = dict(os.environ)
    platform_name = _cfg._tl_get("PLATFORM_NAME")
    if platform_name:
        env["CLIVE_PLATFORM"] = platform_name

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            env=env,
            preexec_fn=os.setsid,
        )
        if on_process is not None:
            on_process(proc)
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")), timeout=timeout
        )
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            _logger().error("claude error (rc=%d): %s", proc.returncode, err)
            err_lower = err.lower()
            if any(kw in err_lower for kw in ("usage limit", "rate limit", "quota", "plan limit", "exceeded")):
                return "現在、プランの使用制限に達しているため利用できません。しばらく時間をおいてから再度お試しください。", False, None
            return f"エラーが発生しました（終了コード {proc.returncode}）:\n```\n{err[:800]}\n```", False, None
        return stdout.decode("utf-8", errors="replace").strip(), False, new_session_id
    except asyncio.TimeoutError:
        if proc is not None:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return "", True, None
    except asyncio.CancelledError:
        if proc is not None:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        raise  # タスクキャンセルは上位に伝搬させる
    except Exception as e:
        _logger().exception("Unexpected engine error: %s", e)
        if proc is not None:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return f"エラーが発生しました: {e}", False, None


async def _run_codex_cli(
    prompt: str,
    model: str,
    thinking: bool,
    timeout: int | None,
    session_id: str | None,
    is_new_session: bool,
    on_process: "Callable[[asyncio.subprocess.Process], None] | None",
    skill_instructions: str,
) -> tuple[str, bool, str | None]:
    """OpenAI Codex CLI を subprocess で実行する。"""
    from core.config import CODEX_BIN
    if timeout is None:
        timeout = TIMEOUT_FAST

    cmd = [CODEX_BIN, "exec"]
    if not is_new_session and session_id:
        cmd += ["resume", session_id]

    if thinking:
        cmd += ["-c", "reasoning_effort=high"]

    if get_skip_permissions():
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd += ["--model", model]

    # [PROMPT] を引数として末尾に追加
    full_prompt = f"{skill_instructions}\n\n{prompt}" if skill_instructions else prompt
    cmd.append(full_prompt)

    _logger().info(
        "engine=codex model=%s prompt_len=%d timeout=%ds",
        model, len(full_prompt), timeout,
    )

    env = dict(os.environ)
    platform_name = _cfg._tl_get("PLATFORM_NAME")
    if platform_name:
        env["CLIVE_PLATFORM"] = platform_name

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            env=env,
            preexec_fn=os.setsid,
        )
        if on_process is not None:
            on_process(proc)

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            _logger().error("codex error (rc=%d): %s", proc.returncode, err)
            err_lower = err.lower()
            if "401 unauthorized" in err_lower or "missing bearer" in err_lower:
                return "OpenAIの認証エラーです。環境変数 OPENAI_API_KEY が設定されているか確認してください。", False, None
            if any(kw in err_lower for kw in ("usage limit", "rate limit", "quota", "plan limit", "exceeded")):
                return "現在、プランの使用制限に達しているため利用できません。しばらく時間をおいてから再度お試しください。", False, None
            return f"エラーが発生しました（終了コード {proc.returncode}）:\n```\n{err[:800]}\n```", False, None

        # Codex CLI の標準出力を直接返す
        out_text = stdout.decode("utf-8", errors="replace").strip()
        
        new_session_id = None
        if is_new_session:
            # session id: 019cc87d-xxxx... のような行を探す
            m = re.search(r"session id:\s*([a-zA-Z0-9\-]+)", out_text, re.IGNORECASE)
            if m:
                new_session_id = m.group(1)

        return out_text, False, new_session_id

    except asyncio.TimeoutError:
        if proc is not None:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return "", True, None
    except asyncio.CancelledError:
        if proc is not None:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        raise  # タスクキャンセルは上位に伝搬させる
    except Exception as e:
        _logger().exception("Unexpected engine error: %s", e)
        if proc is not None:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return f"エラーが発生しました: {e}", False, None


def validate_engine_bin() -> None:
    """設定されたエンジンのバイナリが存在するか確認する。なければ即終了する。"""
    engine = _cfg.get_engine_name()
    if engine == "codex":
        _cfg.validate_codex_bin()
    else:
        # デフォルト: Claude CLI等
        _cfg.validate_engine_bin_path()
