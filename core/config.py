"""
定数・設定管理・スケジュール/テンプレート/チャンネル名の読み書き
"""

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

# スレッドごとに独立したワークスペース設定を保持する
# （Discord/Slack 同時起動時のグローバル変数競合を防ぐ）
_tl = threading.local()

# ─────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
CLAUDE_MD_FILE = BASE_DIR / "CLAUDE.md"
LOG_FILE = BASE_DIR / "bot.log"

# スレッドローカルで管理するワークスペース変数のデフォルト値
_default_ws = BASE_DIR / "workspace"
_WORKSPACE_DEFAULTS: dict[str, object] = {
    "PLATFORM_NAME": "",
    "WORKFLOW_DIR": _default_ws,
    "MEMORY_DIR": _default_ws / "memory",
    "SCHEDULES_FILE": _default_ws / "schedules" / "schedules.json",
    "ATTACHMENTS_DIR": _default_ws / "temp",
    "TMP_DIR": _default_ws / "temp",
    "CHANNEL_NAMES_FILE": _default_ws / "channel_names.json",
    "SESSIONS_FILE": _default_ws / "sessions.json",
    "SOUL_FILE": _default_ws / "SOUL.md",
    "USER_FILE": _default_ws / "USER.md",
}


def init_workspace(workspace_dir: Path) -> None:
    """起動時にプラットフォーム固有の workspace パスをスレッドローカルに設定する。"""
    platform = workspace_dir.parent.name
    _tl.PLATFORM_NAME = platform
    _tl.WORKFLOW_DIR = workspace_dir
    _tl.MEMORY_DIR = workspace_dir / "memory"
    _tl.SCHEDULES_FILE = workspace_dir / "schedules" / "schedules.json"
    _tl.ATTACHMENTS_DIR = workspace_dir / "temp"
    _tl.TMP_DIR = workspace_dir / "temp"
    _tl.CHANNEL_NAMES_FILE = workspace_dir / "channel_names.json"
    _tl.SESSIONS_FILE = workspace_dir / "sessions.json"
    _tl.SOUL_FILE = workspace_dir / "SOUL.md"
    _tl.USER_FILE = workspace_dir / "USER.md"


def _tl_get(attr: str):
    """スレッドローカル変数を返す。未設定の場合はデフォルト値を返す。"""
    return getattr(_tl, attr, _WORKSPACE_DEFAULTS[attr])


def __getattr__(name: str):
    """モジュール属性アクセスをスレッドローカルにディスパッチする。

    _cfg.WORKFLOW_DIR のように直接参照しても、スレッドローカル値が返る。
    """
    if name in _WORKSPACE_DEFAULTS:
        return _tl_get(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


CLAUDE_BIN = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
TIMEOUT_FAST = 180
TIMEOUT_PLANNING = 300

logger = logging.getLogger("discord_bot")


def _atomic_write_json(path: Path, data) -> None:
    """tempfile + os.rename で JSON を安全に書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────
# 設定管理
# ─────────────────────────────────────────────

_config_cache: dict | None = None
_config_mtime: float = 0.0

def load_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mtime = 0.0
    if _config_cache is not None and mtime == _config_mtime:
        return _config_cache
    if not CONFIG_FILE.exists():
        _config_cache = {}
        _config_mtime = 0.0
        return _config_cache
    with open(CONFIG_FILE, encoding="utf-8") as f:
        _config_cache = json.load(f)
    _config_mtime = mtime
    return _config_cache

def get_skip_permissions() -> bool:
    return load_platform_config().get("skip_permissions", True)

def save_config(cfg: dict) -> None:
    global _config_cache, _config_mtime
    _atomic_write_json(CONFIG_FILE, cfg)
    _config_cache = cfg
    _config_mtime = os.path.getmtime(CONFIG_FILE)


# ─────────────────────────────────────────────
# プラットフォーム固有設定（config.json の platforms セクション）
# ─────────────────────────────────────────────

def load_platform_config() -> dict:
    """プラットフォーム固有設定を返す。init_workspace() 前は空 dict。"""
    name = _tl_get("PLATFORM_NAME")
    if not name:
        return {}
    return load_config().get(name, {})

def save_platform_config(cfg: dict) -> None:
    name = _tl_get("PLATFORM_NAME")
    if not name:
        logger.error("save_platform_config: PLATFORM_NAME is not set")
        return
    full_cfg = load_config()
    full_cfg[name] = cfg
    save_config(full_cfg)

def load_schedules() -> list:
    f = _tl_get("SCHEDULES_FILE")
    if not f.exists():
        return []
    try:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []

def save_schedules(schedules: list) -> None:
    _atomic_write_json(_tl_get("SCHEDULES_FILE"), schedules)

def load_channel_names() -> dict:
    f = _tl_get("CHANNEL_NAMES_FILE")
    if not f.exists():
        return {}
    try:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}

def save_channel_name(channel_id: int, name: str) -> None:
    data = load_channel_names()
    if data.get(str(channel_id)) != name:
        data[str(channel_id)] = name
        _atomic_write_json(_tl_get("CHANNEL_NAMES_FILE"), data)

def get_channel_name(channel_id: int) -> str:
    data = load_channel_names()
    return data.get(str(channel_id), str(channel_id))

def get_channel_session(channel_id: int) -> str | None:
    f = _tl_get("SESSIONS_FILE")
    if not f.exists():
        return None
    try:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        return data.get(str(channel_id))
    except (json.JSONDecodeError, ValueError):
        return None

def get_model_config() -> tuple[str, bool]:
    """(model, thinking) を返す。"""
    cfg = load_platform_config()
    return cfg.get("model", "sonnet"), cfg.get("thinking", False)

def get_no_mention_channels() -> set[str]:
    return set(load_platform_config().get("no_mention_channels", []))

def set_no_mention(channel_id: int, enabled: bool) -> None:
    """enabled=True でメンション不要、False で必要に設定。"""
    cfg = load_platform_config()
    channels = list(cfg.get("no_mention_channels", []))
    cid = str(channel_id)
    if enabled and cid not in channels:
        channels.append(cid)
    elif not enabled and cid in channels:
        channels.remove(cid)
    cfg["no_mention_channels"] = channels
    save_platform_config(cfg)

def save_channel_session(channel_id: int, session_id: str) -> None:
    f = _tl_get("SESSIONS_FILE")
    data: dict = {}
    if f.exists():
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, ValueError):
            data = {}
    data[str(channel_id)] = session_id
    _atomic_write_json(f, data)
