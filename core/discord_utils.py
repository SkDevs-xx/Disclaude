"""
Discord ユーティリティ
"""

import discord


def get_guild_channels(guild: discord.Guild) -> list[tuple[int, str]]:
    """ギルドのテキストチャンネル + スレッドを (id, name) のリストで返す。"""
    channels: list[tuple[int, str]] = []
    for ch in guild.channels:
        if isinstance(ch, discord.TextChannel):
            channels.append((ch.id, ch.name))
    for th in guild.threads:
        if isinstance(th.parent, discord.TextChannel):
            channels.append((th.id, f"{th.parent.name} > {th.name}"))
    channels.sort(key=lambda x: x[1])
    return channels
