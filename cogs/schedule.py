"""
ScheduleCog: /schedule グループ（add / wrapup / list）+ Modal / View
"""

import logging
import random
import string
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.triggers.cron import CronTrigger

from core.config import (
    get_channel_name, load_schedules, save_schedules,
)
from core.discord_utils import get_guild_channels
from core.embeds import make_error_embed, make_info_embed

logger = logging.getLogger("discord_bot")

_DAY_MAP = {"月": "MON", "火": "TUE", "水": "WED", "木": "THU", "金": "FRI", "土": "SAT", "日": "SUN"}
_DAY_REV = {v: k for k, v in _DAY_MAP.items()}

_FREQ_LABELS = {
    "daily":    "毎日（時刻指定）",
    "weekday":  "平日のみ（月〜金）",
    "weekly":   "毎週（曜日・時刻指定）",
    "hourly":   "毎時（分指定）",
    "interval": "N分ごと",
}


# ─────────────────────────────────────────────
# cron パーサー / 推論ヘルパー
# ─────────────────────────────────────────────
def _parse_cron(freq: str, values: dict) -> str | None:
    """freq と入力値から cron 式を生成。不正なら None。"""
    try:
        if freq in ("daily", "weekday", "weekly"):
            h, m = values["time"].strip().split(":")
            h, m = int(h), int(m)
            if freq == "daily":
                cron = f"{m} {h} * * *"
            elif freq == "weekday":
                cron = f"{m} {h} * * MON-FRI"
            else:
                day = _DAY_MAP.get(values["day"].strip())
                if not day:
                    return None
                cron = f"{m} {h} * * {day}"
        elif freq == "hourly":
            m = int(values["minute"].strip())
            if not 0 <= m <= 59:
                return None
            cron = f"{m} * * * *"
        elif freq == "interval":
            n = int(values["interval"].strip())
            if n < 1:
                return None
            cron = f"*/{n} * * * *"
        else:
            return None
        CronTrigger.from_crontab(cron)
        return cron
    except Exception:
        return None


def _infer_freq_from_cron(cron: str) -> str | None:
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    m, h, dom, mon, dow = parts
    if m.startswith("*/"):
        return "interval"
    if h == "*" and dom == "*" and mon == "*" and dow == "*":
        return "hourly"
    if dom == "*" and mon == "*" and dow == "MON-FRI":
        return "weekday"
    if dom == "*" and mon == "*" and dow == "*":
        return "daily"
    if dom == "*" and mon == "*":
        return "weekly"
    return None


def _cron_to_fields(cron: str, freq: str) -> dict:
    try:
        m, h, dom, mon, dow = cron.strip().split()
        if freq in ("daily", "weekday"):
            return {"time": f"{int(h):02d}:{int(m):02d}"}
        if freq == "weekly":
            return {"time": f"{int(h):02d}:{int(m):02d}", "day": _DAY_REV.get(dow.upper(), "月")}
        if freq == "hourly":
            return {"minute": str(int(m))}
        if freq == "interval":
            return {"interval": m.removeprefix("*/")}
    except Exception:
        pass
    return {}


# ─────────────────────────────────────────────
# 汎用スケジュール Modal（追加）
# ─────────────────────────────────────────────
class ScheduleAddModal(discord.ui.Modal):
    """頻度タイプに応じたフィールドを動的に追加するスケジュール追加 Modal"""

    def __init__(self, bot: commands.Bot, channel_id: int, freq: str, model: str = "sonnet", thinking: bool = False):
        super().__init__(title=f"スケジュール追加（{_FREQ_LABELS[freq]}）")
        self.bot = bot
        self.channel_id = channel_id
        self.freq = freq
        self.model = model
        self.thinking = thinking

        self.sched_name = discord.ui.TextInput(label="スケジュール名", max_length=100)
        self.prompt = discord.ui.TextInput(label="実行プロンプト", style=discord.TextStyle.paragraph, max_length=4000)
        self.add_item(self.sched_name)
        self.add_item(self.prompt)

        self.time_input = self.day_input = self.minute_input = self.interval_input = None
        if freq in ("daily", "weekday", "weekly"):
            self.time_input = discord.ui.TextInput(label="実行時刻（HH:MM）", placeholder="09:00", max_length=5)
            self.add_item(self.time_input)
        if freq == "weekly":
            self.day_input = discord.ui.TextInput(label="曜日（月火水木金土日）", placeholder="月", max_length=1)
            self.add_item(self.day_input)
        if freq == "hourly":
            self.minute_input = discord.ui.TextInput(label="何分に実行（0〜59）", placeholder="0", max_length=2)
            self.add_item(self.minute_input)
        if freq == "interval":
            self.interval_input = discord.ui.TextInput(label="実行間隔（分）", placeholder="30", max_length=3)
            self.add_item(self.interval_input)

    def _get_values(self) -> dict:
        vals = {}
        if self.time_input:
            vals["time"] = self.time_input.value
        if self.day_input:
            vals["day"] = self.day_input.value
        if self.minute_input:
            vals["minute"] = self.minute_input.value
        if self.interval_input:
            vals["interval"] = self.interval_input.value
        return vals

    async def on_submit(self, interaction: discord.Interaction):
        cron = _parse_cron(self.freq, self._get_values())
        if cron is None:
            await interaction.response.send_message(
                embed=make_error_embed("入力の形式が正しくありません。"), ephemeral=True
            )
            return
        await _commit_schedule(self.bot, interaction, self.sched_name.value, cron, self.prompt.value, self.channel_id, self.model, self.thinking)


# ─────────────────────────────────────────────
# 汎用スケジュール Modal（編集）
# ─────────────────────────────────────────────
class ScheduleEditModal(discord.ui.Modal):
    """頻度タイプに応じたフィールドを動的に追加するスケジュール編集 Modal"""

    def __init__(self, bot: commands.Bot, schedule: dict, channel_id: int, freq: str, fields: dict, model: str = "sonnet", thinking: bool = False):
        super().__init__(title=f"スケジュール編集（{_FREQ_LABELS[freq]}）")
        self.bot = bot
        self.schedule_id = schedule["id"]
        self.channel_id = channel_id
        self.freq = freq
        self.model = model
        self.thinking = thinking

        self.sched_name = discord.ui.TextInput(label="スケジュール名", default=schedule["name"], max_length=100)
        self.prompt = discord.ui.TextInput(label="実行プロンプト", style=discord.TextStyle.paragraph, default=schedule["prompt"], max_length=4000)
        self.add_item(self.sched_name)
        self.add_item(self.prompt)

        self.time_input = self.day_input = self.minute_input = self.interval_input = None
        if freq in ("daily", "weekday", "weekly"):
            self.time_input = discord.ui.TextInput(label="実行時刻（HH:MM）", default=fields.get("time", "09:00"), placeholder="09:00", max_length=5)
            self.add_item(self.time_input)
        if freq == "weekly":
            self.day_input = discord.ui.TextInput(label="曜日（月火水木金土日）", default=fields.get("day", "月"), placeholder="月", max_length=1)
            self.add_item(self.day_input)
        if freq == "hourly":
            self.minute_input = discord.ui.TextInput(label="何分に実行（0〜59）", default=fields.get("minute", "0"), placeholder="0", max_length=2)
            self.add_item(self.minute_input)
        if freq == "interval":
            self.interval_input = discord.ui.TextInput(label="実行間隔（分）", default=fields.get("interval", "30"), placeholder="30", max_length=3)
            self.add_item(self.interval_input)

    def _get_values(self) -> dict:
        vals = {}
        if self.time_input:
            vals["time"] = self.time_input.value
        if self.day_input:
            vals["day"] = self.day_input.value
        if self.minute_input:
            vals["minute"] = self.minute_input.value
        if self.interval_input:
            vals["interval"] = self.interval_input.value
        return vals

    async def on_submit(self, interaction: discord.Interaction):
        cron = _parse_cron(self.freq, self._get_values())
        if cron is None:
            await interaction.response.send_message(
                embed=make_error_embed("入力の形式が正しくありません。"), ephemeral=True
            )
            return
        await _apply_schedule_edit(self.bot, interaction, self.schedule_id, self.sched_name.value, self.prompt.value, cron, self.channel_id, self.model, self.thinking)


# ─────────────────────────────────────────────
# チャンネル選択 + 頻度選択 View
# ─────────────────────────────────────────────
class ScheduleSetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot, channels: list[tuple[int, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.selected_channel_id: int | None = None
        self.selected_channel_name: str | None = None
        self.selected_freq: str | None = None
        self.selected_model: str = "sonnet"
        self.selected_thinking: bool = False

        ch_options = [
            discord.SelectOption(label=f"# {name}", value=str(cid))
            for cid, name in channels[:25]
        ]
        self.ch_select = discord.ui.Select(placeholder="① 投稿先チャンネル / スレッドを選択", options=ch_options)
        self.ch_select.callback = self._on_channel
        self.add_item(self.ch_select)

        freq_options = [
            discord.SelectOption(label="毎日（時刻指定）",       value="daily",    description="例: 毎日 09:00"),
            discord.SelectOption(label="平日のみ（月〜金）",     value="weekday",  description="例: 平日 09:00"),
            discord.SelectOption(label="毎週（曜日・時刻指定）", value="weekly",   description="例: 毎週月曜 09:00"),
            discord.SelectOption(label="毎時（分指定）",         value="hourly",   description="例: 毎時0分"),
            discord.SelectOption(label="N分ごと",               value="interval", description="例: 30分ごと"),
        ]
        self.freq_select = discord.ui.Select(placeholder="② 実行頻度を選択", options=freq_options)
        self.freq_select.callback = self._on_freq
        self.add_item(self.freq_select)

        model_options = [
            discord.SelectOption(label="Sonnet", value="sonnet", description="高速・バランス型（デフォルト）", default=True),
            discord.SelectOption(label="Opus", value="opus", description="最高精度・低速"),
            discord.SelectOption(label="Haiku", value="haiku", description="最速・軽量"),
        ]
        self.model_select = discord.ui.Select(placeholder="③ モデルを選択", options=model_options)
        self.model_select.callback = self._on_model
        self.add_item(self.model_select)

        thinking_options = [
            discord.SelectOption(label="Thinking OFF", value="false", description="通常モード（デフォルト）", default=True),
            discord.SelectOption(label="Thinking ON", value="true", description="深い思考モード（effort: high）"),
        ]
        self.thinking_select = discord.ui.Select(placeholder="④ Thinking を選択", options=thinking_options)
        self.thinking_select.callback = self._on_thinking
        self.add_item(self.thinking_select)

        self.next_btn = discord.ui.Button(label="⑤ 次へ →", style=discord.ButtonStyle.primary)
        self.next_btn.callback = self._on_next
        self.add_item(self.next_btn)

    def make_embed(self) -> discord.Embed:
        ch_line  = f"✅ チャンネル: **#{self.selected_channel_name}**" if self.selected_channel_name else "⬜ ① チャンネルを選択してください"
        freq_line = f"✅ 頻度: **{_FREQ_LABELS.get(self.selected_freq, '')}**" if self.selected_freq else "⬜ ② 実行頻度を選択してください"
        model_line = f"✅ モデル: **{self.selected_model}**"
        thinking_line = f"✅ Thinking: **{'ON' if self.selected_thinking else 'OFF'}**"
        hint = "\n\n全て選択したら **⑤ 次へ →** を押してください。" if not (self.selected_channel_name and self.selected_freq) else "\n\n**⑤ 次へ →** を押してモーダルに詳細を入力してください。"
        return discord.Embed(
            title="スケジュール追加",
            description=f"{ch_line}\n{freq_line}\n{model_line}\n{thinking_line}{hint}",
            color=discord.Color.blue(),
        )

    async def _on_channel(self, interaction: discord.Interaction):
        self.selected_channel_id = int(interaction.data["values"][0])
        for opt in self.ch_select.options:
            opt.default = (opt.value == str(self.selected_channel_id))
            if opt.default:
                self.selected_channel_name = opt.label.removeprefix("# ")
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_freq(self, interaction: discord.Interaction):
        self.selected_freq = interaction.data["values"][0]
        for opt in self.freq_select.options:
            opt.default = (opt.value == self.selected_freq)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_model(self, interaction: discord.Interaction):
        self.selected_model = interaction.data["values"][0]
        for opt in self.model_select.options:
            opt.default = (opt.value == self.selected_model)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_thinking(self, interaction: discord.Interaction):
        self.selected_thinking = interaction.data["values"][0] == "true"
        for opt in self.thinking_select.options:
            opt.default = (opt.value == str(self.selected_thinking).lower())
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if not self.selected_channel_id:
            await interaction.response.send_message(embed=make_error_embed("① チャンネルを選択してください。"), ephemeral=True)
            return
        if not self.selected_freq:
            await interaction.response.send_message(embed=make_error_embed("② 実行頻度を選択してください。"), ephemeral=True)
            return
        await interaction.response.send_modal(ScheduleAddModal(self.bot, self.selected_channel_id, self.selected_freq, self.selected_model, self.selected_thinking))


# ─────────────────────────────────────────────
# スケジュール編集 SetupView
# ─────────────────────────────────────────────
class ScheduleEditSetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot, schedule: dict, channels: list[tuple[int, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.schedule = schedule
        self.selected_channel_id = int(schedule["channel_id"])
        self.selected_channel_name = get_channel_name(self.selected_channel_id)
        self.selected_freq = _infer_freq_from_cron(schedule["cron"]) or "daily"
        # 後方互換: 旧 "mode" キーから変換
        self.selected_model = schedule.get("model", "sonnet")
        self.selected_thinking = schedule.get("thinking", schedule.get("mode") == "planning")

        ch_options = [
            discord.SelectOption(label=f"# {name}", value=str(cid), default=(cid == self.selected_channel_id))
            for cid, name in channels[:25]
        ]
        self.ch_select = discord.ui.Select(placeholder="投稿先チャンネル / スレッド", options=ch_options)
        self.ch_select.callback = self._on_channel
        self.add_item(self.ch_select)

        freq_options = [
            discord.SelectOption(label="毎日（時刻指定）",       value="daily",    description="例: 毎日 09:00",     default=(self.selected_freq == "daily")),
            discord.SelectOption(label="平日のみ（月〜金）",     value="weekday",  description="例: 平日 09:00",     default=(self.selected_freq == "weekday")),
            discord.SelectOption(label="毎週（曜日・時刻指定）", value="weekly",   description="例: 毎週月曜 09:00", default=(self.selected_freq == "weekly")),
            discord.SelectOption(label="毎時（分指定）",         value="hourly",   description="例: 毎時0分",        default=(self.selected_freq == "hourly")),
            discord.SelectOption(label="N分ごと",               value="interval", description="例: 30分ごと",       default=(self.selected_freq == "interval")),
        ]
        self.freq_select = discord.ui.Select(placeholder="実行頻度", options=freq_options)
        self.freq_select.callback = self._on_freq
        self.add_item(self.freq_select)

        model_options = [
            discord.SelectOption(label="Sonnet", value="sonnet", description="高速・バランス型", default=(self.selected_model == "sonnet")),
            discord.SelectOption(label="Opus", value="opus", description="最高精度・低速", default=(self.selected_model == "opus")),
            discord.SelectOption(label="Haiku", value="haiku", description="最速・軽量", default=(self.selected_model == "haiku")),
        ]
        self.model_select = discord.ui.Select(placeholder="モデル", options=model_options)
        self.model_select.callback = self._on_model
        self.add_item(self.model_select)

        thinking_options = [
            discord.SelectOption(label="Thinking OFF", value="false", description="通常モード", default=(not self.selected_thinking)),
            discord.SelectOption(label="Thinking ON", value="true", description="深い思考モード（effort: high）", default=self.selected_thinking),
        ]
        self.thinking_select = discord.ui.Select(placeholder="Thinking", options=thinking_options)
        self.thinking_select.callback = self._on_thinking
        self.add_item(self.thinking_select)

        next_btn = discord.ui.Button(label="次へ →", style=discord.ButtonStyle.primary)
        next_btn.callback = self._on_next
        self.add_item(next_btn)

    def make_embed(self) -> discord.Embed:
        freq_label = _FREQ_LABELS.get(self.selected_freq, self.selected_freq)
        return discord.Embed(
            title="スケジュール編集",
            description=(
                f"**{self.schedule['name']}**\n\n"
                f"✅ チャンネル: **#{self.selected_channel_name}**\n"
                f"✅ 頻度: **{freq_label}**\n"
                f"✅ モデル: **{self.selected_model}**\n"
                f"✅ Thinking: **{'ON' if self.selected_thinking else 'OFF'}**\n\n"
                "変更したい項目を選択して **次へ →** を押してください。"
            ),
            color=discord.Color.orange(),
        )

    async def _on_channel(self, interaction: discord.Interaction):
        self.selected_channel_id = int(interaction.data["values"][0])
        for opt in self.ch_select.options:
            opt.default = (opt.value == str(self.selected_channel_id))
            if opt.default:
                self.selected_channel_name = opt.label.removeprefix("# ")
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_freq(self, interaction: discord.Interaction):
        self.selected_freq = interaction.data["values"][0]
        for opt in self.freq_select.options:
            opt.default = (opt.value == self.selected_freq)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_model(self, interaction: discord.Interaction):
        self.selected_model = interaction.data["values"][0]
        for opt in self.model_select.options:
            opt.default = (opt.value == self.selected_model)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_thinking(self, interaction: discord.Interaction):
        self.selected_thinking = interaction.data["values"][0] == "true"
        for opt in self.thinking_select.options:
            opt.default = (opt.value == str(self.selected_thinking).lower())
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        current_freq = _infer_freq_from_cron(self.schedule["cron"])
        fields = _cron_to_fields(self.schedule["cron"], self.selected_freq) if current_freq == self.selected_freq else {}
        await interaction.response.send_modal(
            ScheduleEditModal(self.bot, self.schedule, self.selected_channel_id, self.selected_freq, fields, self.selected_model, self.selected_thinking)
        )


# ─────────────────────────────────────────────
# スケジュール操作 View（今すぐ実行・編集・一時停止・削除）
# ─────────────────────────────────────────────
class ScheduleActionView(discord.ui.View):
    def __init__(self, bot: commands.Bot, schedule_id: str, schedule_name: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.schedule_id = schedule_id
        self.schedule_name = schedule_name

    @discord.ui.button(label="今すぐ実行", style=discord.ButtonStyle.success)
    async def run_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        schedules = load_schedules()
        target = next((s for s in schedules if s["id"] == self.schedule_id), None)
        if not target:
            await interaction.followup.send(embed=make_error_embed("スケジュールが見つかりません。"), ephemeral=True)
            return
        await self.bot._run_schedule(target)
        await interaction.followup.send(
            embed=make_info_embed("即時実行", f"**{self.schedule_name}** を実行しました。"), ephemeral=True
        )

    @discord.ui.button(label="編集", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedules = load_schedules()
        target = next((s for s in schedules if s["id"] == self.schedule_id), None)
        if not target:
            await interaction.response.send_message(embed=make_error_embed("スケジュールが見つかりません。"), ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message(embed=make_error_embed("サーバー内でのみ使用できます。"), ephemeral=True)
            return
        channels = get_guild_channels(interaction.guild)
        view = ScheduleEditSetupView(self.bot, target, channels)
        await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="一時停止", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedules = load_schedules()
        for s in schedules:
            if s["id"] == self.schedule_id:
                s["status"] = "paused"
        save_schedules(schedules)
        self.bot._reload_schedules()
        await interaction.response.send_message(
            embed=make_info_embed("スケジュール", f"**{self.schedule_name}** を一時停止しました。"), ephemeral=True
        )

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedules = [s for s in load_schedules() if s["id"] != self.schedule_id]
        save_schedules(schedules)
        self.bot._reload_schedules()
        await interaction.response.send_message(
            embed=make_info_embed("スケジュール", f"**{self.schedule_name}** を削除しました。"), ephemeral=True
        )


# ─────────────────────────────────────────────
# ヘルパー関数
# ─────────────────────────────────────────────


async def _commit_schedule(bot: commands.Bot, interaction: discord.Interaction, name: str, cron: str, prompt: str, channel_id: int, model: str = "sonnet", thinking: bool = False):
    try:
        new_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        schedules = load_schedules()
        schedules.append({
            "id": new_id, "name": name, "cron": cron,
            "prompt": prompt, "channel_id": str(channel_id),
            "model": model, "thinking": thinking,
            "status": "active", "run_count": 0, "last_run": None,
        })
        save_schedules(schedules)
        bot._reload_schedules()
        ch_name = get_channel_name(channel_id)
        thinking_label = "ON" if thinking else "OFF"
        await interaction.response.send_message(
            embed=make_info_embed(
                "スケジュール追加",
                f"**{name}** を追加しました\n**Cron:** `{cron}`\n**モデル:** {model}\n**Thinking:** {thinking_label}\n**チャンネル:** #{ch_name}",
            ),
            ephemeral=True,
        )
    except Exception as e:
        logger.exception("_commit_schedule error: %s", e)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(f"スケジュール保存に失敗しました:\n```{e}```"), ephemeral=True
            )


async def _apply_schedule_edit(bot: commands.Bot, interaction: discord.Interaction, schedule_id: str, name: str, prompt: str, cron: str, channel_id: int, model: str = "sonnet", thinking: bool = False):
    try:
        schedules = load_schedules()
        for s in schedules:
            if s["id"] == schedule_id:
                s["name"] = name
                s["prompt"] = prompt
                s["cron"] = cron
                s["channel_id"] = str(channel_id)
                s["model"] = model
                s["thinking"] = thinking
                s.pop("mode", None)  # 旧キー削除
        save_schedules(schedules)
        bot._reload_schedules()
        ch_name = get_channel_name(channel_id)
        thinking_label = "ON" if thinking else "OFF"
        await interaction.response.send_message(
            embed=make_info_embed("スケジュール編集", f"**{name}** を更新しました。\n**モデル:** {model}\n**Thinking:** {thinking_label}\n**チャンネル:** #{ch_name}"),
            ephemeral=True,
        )
    except Exception as e:
        logger.exception("_apply_schedule_edit error: %s", e)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(f"スケジュール更新に失敗しました:\n```{e}```"), ephemeral=True
            )


# ─────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────
class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    schedule = app_commands.Group(name="schedule", description="スケジュール管理")

    @schedule.command(name="add", description="スケジュールを追加する")
    async def schedule_add(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(embed=make_error_embed("サーバー内でのみ使用できます。"), ephemeral=True)
            return
        channels = get_guild_channels(interaction.guild)
        if not channels:
            await interaction.response.send_message(embed=make_error_embed("チャンネルが見つかりません。"), ephemeral=True)
            return
        view = ScheduleSetupView(self.bot, channels)
        await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)

    @schedule.command(name="list", description="スケジュール一覧を表示する")
    async def schedule_list(self, interaction: discord.Interaction):
        schedules = load_schedules()
        if not schedules:
            await interaction.response.send_message(
                embed=make_info_embed("スケジュール一覧", "スケジュールはまだありません。"), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        for s in schedules:
            last_raw = s.get("last_run")
            if last_raw:
                try:
                    from datetime import datetime as _dt
                    from zoneinfo import ZoneInfo
                    last = _dt.fromisoformat(last_raw).astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    last = last_raw
            else:
                last = "未実行"
            is_wrapup = s.get("type") == "wrapup"
            prompt_line = "（ラップアップ自動実行）" if is_wrapup else s["prompt"][:80]
            model_label = s.get("model", "sonnet")
            thinking_label = "ON" if s.get("thinking", s.get("mode") == "planning") else "OFF"
            desc = (
                f"**Cron:** `{s['cron']}`\n"
                f"**内容:** {prompt_line}\n"
                f"**チャンネル:** #{get_channel_name(int(s['channel_id']))}\n"
                f"**モデル:** {model_label} / **Thinking:** {thinking_label}\n"
                f"**状態:** {s['status']}\n"
                f"**実行回数:** {s.get('run_count', 0)}\n"
                f"**最終実行:** {last}"
            )
            embed = discord.Embed(title=s["name"], description=desc, color=discord.Color.blurple())
            view = ScheduleActionView(self.bot, s["id"], s["name"])
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
