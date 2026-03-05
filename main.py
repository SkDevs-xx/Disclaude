"""
マルチプラットフォーム対応エントリポイント
Usage:
    python main.py
    python main.py --init-workspace slack --from discord
"""

import argparse
import logging
import os
import shutil
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from core.config import BASE_DIR, LOG_FILE, init_workspace

logger = logging.getLogger("discord_bot")


def _setup_logging():
    """ロガーを設定する。"""
    logger.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)


def _init_workspace_cmd(platform: str, from_platform: str):
    """既存プラットフォームの workspace を新プラットフォームにコピーする。"""
    src = BASE_DIR / "platforms" / from_platform / "workspace"
    dst = BASE_DIR / "platforms" / platform / "workspace"
    if not src.exists():
        print(f"Error: source workspace not found: {src}")
        sys.exit(1)
    if dst.exists():
        print(f"Error: destination workspace already exists: {dst}")
        print("Delete it first if you want to reinitialize.")
        sys.exit(1)
    # platforms/{platform}/ ディレクトリを作成
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    print(f"Workspace copied: {src} -> {dst}")
    print("Edit SOUL.md to adjust the personality for the new platform.")


def _run_discord():
    """Discord Bot を起動する。"""
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token or token == "your_token_here":
        logger.error(".env に DISCORD_BOT_TOKEN が設定されていません。")
        sys.exit(1)

    workspace_dir = BASE_DIR / "platforms" / "discord" / "workspace"
    init_workspace(workspace_dir)

    # workspace ディレクトリを作成
    from core.config import WORKFLOW_DIR, MEMORY_DIR, ATTACHMENTS_DIR, TMP_DIR
    for d in [WORKFLOW_DIR, MEMORY_DIR, ATTACHMENTS_DIR, TMP_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    from platforms.discord.bot import ClaudeBot
    bot = ClaudeBot()
    bot.run(token, log_handler=None)


def _run_slack():
    """Slack Bot を起動する。"""
    import asyncio
    load_dotenv(BASE_DIR / ".env")
    bot_token = os.getenv("SLACK_BOT_TOKEN")
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not bot_token:
        logger.error(".env に SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)
    if not app_token:
        logger.error(".env に SLACK_APP_TOKEN が設定されていません。")
        sys.exit(1)

    workspace_dir = BASE_DIR / "platforms" / "slack" / "workspace"
    init_workspace(workspace_dir)

    # workspace ディレクトリを作成
    from core.config import WORKFLOW_DIR, MEMORY_DIR, ATTACHMENTS_DIR, TMP_DIR
    for d in [WORKFLOW_DIR, MEMORY_DIR, ATTACHMENTS_DIR, TMP_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    from platforms.slack.bot import SlackBot
    bot = SlackBot(bot_token, app_token)
    asyncio.run(bot.start())


def main():
    parser = argparse.ArgumentParser(description="Multi-platform Claude Bot")
    parser.add_argument("--init-workspace", metavar="PLATFORM",
                        help="Initialize workspace for a new platform")
    parser.add_argument("--from", dest="from_platform", metavar="PLATFORM",
                        help="Source platform to copy workspace from (used with --init-workspace)")
    args = parser.parse_args()

    _setup_logging()

    if args.init_workspace:
        if not args.from_platform:
            parser.error("--from is required with --init-workspace")
        _init_workspace_cmd(args.init_workspace, args.from_platform)
        return

    from core.config import load_config
    cfg = load_config()

    slack_enabled = cfg.get("slack", {}).get("enabled", False)
    discord_enabled = cfg.get("discord", {}).get("enabled", False)

    if not slack_enabled and not discord_enabled:
        logger.error(
            "有効なプラットフォームがありません。config.json の discord.enabled または slack.enabled を true に設定してください。"
        )
        sys.exit(1)

    if slack_enabled and discord_enabled:
        # 両方有効な場合はスレッドで並列起動
        t = threading.Thread(target=_run_discord, daemon=True)
        t.start()
        _run_slack()  # メインスレッドで Slack を起動
    elif slack_enabled:
        _run_slack()
    else:
        _run_discord()


if __name__ == "__main__":
    main()
