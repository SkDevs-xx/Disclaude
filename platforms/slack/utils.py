"""
Slack ユーティリティ
"""

import logging
from datetime import datetime

from zoneinfo import ZoneInfo

from core.wrapup import CollectedMessages, WRAPUP_CHAR_CAP

logger = logging.getLogger("slack_bot")
JST = ZoneInfo("Asia/Tokyo")


def make_slack_collector(client, team_id: str | None = None):
    """Slack ワークスペース用のメッセージ収集関数を返す。"""

    async def collect(after_dt: datetime, before_dt: datetime) -> CollectedMessages:
        parts: dict[str, list[str]] = {}
        total_chars = 0
        total_msgs = 0
        truncated = False

        # ── 全チャンネルを取得 ──
        channels: list[dict] = []
        cursor = None
        while True:
            try:
                kwargs: dict = {"types": "public_channel", "exclude_archived": True, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                result = await client.conversations_list(**kwargs)
                channels.extend(result.get("channels", []))
                next_cursor = result.get("response_metadata", {}).get("next_cursor", "")
                if not next_cursor:
                    break
                cursor = next_cursor
            except Exception as e:
                logger.warning("wrapup: conversations_list error: %s", e)
                break

        # ── 各チャンネルのメッセージを収集 ──
        oldest = str(after_dt.timestamp())
        latest = str(before_dt.timestamp())

        for ch in channels:
            if truncated:
                break
            ch_id = ch["id"]
            ch_name = ch.get("name", ch_id)
            try:
                msg_cursor = None
                while True:
                    kwargs = {
                        "channel": ch_id,
                        "oldest": oldest,
                        "latest": latest,
                        "inclusive": True,
                        "limit": 200,
                    }
                    if msg_cursor:
                        kwargs["cursor"] = msg_cursor
                    resp = await client.conversations_history(**kwargs)
                    messages = resp.get("messages", [])
                    for msg in reversed(messages):  # 古い順
                        if msg.get("subtype") or not msg.get("text"):
                            continue
                        ts_float = float(msg["ts"])
                        dt = datetime.fromtimestamp(ts_float, tz=JST)
                        ts_str = dt.strftime("%Y-%m-%d %H:%M")
                        user = msg.get("username") or msg.get("user", "unknown")
                        line = f"[{ts_str}] {user}: {msg['text']}"
                        total_chars += len(line) + 1
                        total_msgs += 1
                        parts.setdefault(ch_name, []).append(line)
                        if total_chars >= WRAPUP_CHAR_CAP:
                            truncated = True
                            break
                    if truncated:
                        break
                    next_cursor = resp.get("response_metadata", {}).get("next_cursor", "")
                    if not next_cursor or not resp.get("has_more", False):
                        break
                    msg_cursor = next_cursor
            except Exception as e:
                logger.warning("wrapup: error fetching #%s: %s", ch_name, e)

        return CollectedMessages(parts, total_chars, total_msgs, truncated)

    return collect


async def get_workspace_channels(client) -> list[tuple[str, str]]:
    """ワークスペースの公開チャンネルを (id, name) のリストで返す。"""
    channels: list[tuple[str, str]] = []
    cursor = None
    while True:
        try:
            kwargs: dict = {"types": "public_channel", "exclude_archived": True, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            result = await client.conversations_list(**kwargs)
            for ch in result.get("channels", []):
                channels.append((ch["id"], ch.get("name", ch["id"])))
            next_cursor = result.get("response_metadata", {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor
        except Exception as e:
            logger.warning("get_workspace_channels error: %s", e)
            break
    channels.sort(key=lambda x: x[1])
    return channels
