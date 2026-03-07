"""
UtilityCog: /model, /status, /cancel, /mention, /skills-list
"""

import re
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from core.config import (
    load_platform_config, save_platform_config,
    get_model_config,
    get_no_mention_channels, set_no_mention,
    get_channel_session, save_channel_session, delete_channel_session,
    BASE_DIR,
)
from core.engine import run_engine
from core.message import split_message
from platforms.discord.embeds import make_info_embed, make_error_embed


MODEL_CHOICES = [
    discord.SelectOption(label="Sonnet", value="sonnet", description="高速・バランス型（デフォルト）"),
    discord.SelectOption(label="Opus", value="opus", description="最高精度・低速"),
    discord.SelectOption(label="Haiku", value="haiku", description="最速・軽量"),
]


class ModelView(discord.ui.View):
    """モデル + Thinking 設定の Embed + Select + ボタン。"""

    def __init__(self, current_model: str, current_thinking: bool):
        super().__init__(timeout=60)
        self.current_model = current_model
        self.current_thinking = current_thinking

        options = [
            discord.SelectOption(
                label=opt.label, value=opt.value,
                description=opt.description,
                default=(opt.value == current_model),
            )
            for opt in MODEL_CHOICES
        ]
        self.model_select = discord.ui.Select(placeholder="モデルを選択", options=options)
        self.model_select.callback = self._on_model_select
        self.add_item(self.model_select)

        self._update_buttons(current_thinking)

    def _update_buttons(self, thinking: bool):
        self.thinking_on_btn.style = discord.ButtonStyle.success if thinking else discord.ButtonStyle.secondary
        self.thinking_off_btn.style = discord.ButtonStyle.success if not thinking else discord.ButtonStyle.secondary

    @staticmethod
    def make_embed(model: str, thinking: bool) -> discord.Embed:
        thinking_label = "ON" if thinking else "OFF"
        return make_info_embed(
            "モデル設定",
            f"**モデル:** {model}\n**Thinking:** {thinking_label}",
        )

    async def _on_model_select(self, interaction: discord.Interaction):
        self.current_model = interaction.data["values"][0]
        for opt in self.model_select.options:
            opt.default = (opt.value == self.current_model)
        cfg = load_platform_config()
        cfg["model"] = self.current_model
        save_platform_config(cfg)
        await interaction.response.edit_message(
            embed=self.make_embed(self.current_model, self.current_thinking), view=self
        )

    @discord.ui.button(label="Thinking ON")
    async def thinking_on_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_thinking = True
        self._update_buttons(True)
        cfg = load_platform_config()
        cfg["thinking"] = True
        save_platform_config(cfg)
        await interaction.response.edit_message(
            embed=self.make_embed(self.current_model, True), view=self
        )

    @discord.ui.button(label="Thinking OFF")
    async def thinking_off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_thinking = False
        self._update_buttons(False)
        cfg = load_platform_config()
        cfg["thinking"] = False
        save_platform_config(cfg)
        await interaction.response.edit_message(
            embed=self.make_embed(self.current_model, False), view=self
        )


class MentionView(discord.ui.View):
    """メンション要否の ON/OFF ボタン付きビュー。"""

    def __init__(self, channel_id: int, mention_free: bool):
        super().__init__(timeout=60)
        self.channel_id = channel_id
        self._update_buttons(mention_free)

    def _update_buttons(self, mention_free: bool):
        self.on_btn.style = discord.ButtonStyle.success if mention_free else discord.ButtonStyle.secondary
        self.off_btn.style = discord.ButtonStyle.success if not mention_free else discord.ButtonStyle.secondary

    @staticmethod
    def make_embed(mention_free: bool) -> discord.Embed:
        status = "メンション不要" if mention_free else "メンション必要"
        return make_info_embed("メンション設定", f"このチャンネル: **{status}**")

    @discord.ui.button(label="メンション不要")
    async def on_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_no_mention(self.channel_id, True)
        self._update_buttons(True)
        await interaction.response.edit_message(embed=self.make_embed(True), view=self)

    @discord.ui.button(label="メンション必要")
    async def off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        set_no_mention(self.channel_id, False)
        self._update_buttons(False)
        await interaction.response.edit_message(embed=self.make_embed(False), view=self)


class SkillsListView(discord.ui.View):
    """user-invocable スキルをボタン一覧で表示し、選択時に発動するビュー。"""

    def __init__(self, bot, skills):
        super().__init__(timeout=300)
        self.bot = bot
        for skill in skills[:25]:  # Discord 上限: 5行 × 5ボタン = 25
            btn = discord.ui.Button(
                label=skill.name,
                style=discord.ButtonStyle.primary,
                custom_id=f"skill_invoke__{skill.name}",
            )
            btn.callback = self._make_callback(skill.name)
            self.add_item(btn)

    def _make_callback(self, skill_name: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            channel_id = interaction.channel_id
            channel = interaction.channel or self.bot.get_channel(channel_id)

            try:
                lock = self.bot.get_channel_lock(channel_id)
                async with lock:
                    session_id = get_channel_session(channel_id)
                    is_new = session_id is None
                    if is_new:
                        session_id = str(uuid.uuid4())
                        save_channel_session(channel_id, session_id)

                    model, thinking = get_model_config()
                    registry_instr = self.bot.skill_registry.build_instructions(
                        self.bot.platform_context.name,
                        disabled=self.bot.platform_context.disabled_skills,
                    )
                    skill_instr = (
                        f"[platform: {self.bot.platform_context.name}]\n"
                        + (f"\n{registry_instr}" if registry_instr else "")
                    )
                    prompt = f"[{skill_name}スキルを呼び出してください。スキルの指示に従って会話を開始してください。]"

                    if channel:
                        await channel.typing()
                    response, timed_out = await run_engine(
                        prompt,
                        model=model,
                        thinking=thinking,
                        session_id=session_id,
                        is_new_session=is_new,
                        on_process=lambda p: self.bot.running_processes.__setitem__(channel_id, p),
                        skill_instructions=skill_instr,
                    )

                if timed_out:
                    if channel:
                        await channel.send(embed=make_error_embed(
                            "タイムアウトしました。`/cancel` で再試行するか、少し待ってから再送してください。"
                        ))
                    return

                if response and channel:
                    display_response = re.sub(r'\n{2,}', '\n', response)
                    chunks = split_message(display_response, max_len=2000)
                    await channel.send(chunks[0])
                    for chunk in chunks[1:]:
                        await channel.send(chunk)
            except Exception as e:
                if channel:
                    await channel.send(embed=make_error_embed(f"エラーが発生しました: {e}"))
        return callback


class UtilityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="model", description="モデルと Thinking を設定する")
    async def model_command(self, interaction: discord.Interaction):
        model, thinking = get_model_config()
        view = ModelView(model, thinking)
        await interaction.response.send_message(
            embed=ModelView.make_embed(model, thinking), view=view, ephemeral=True
        )

    @app_commands.command(name="status", description="現在のステータスを表示する")
    async def status_command(self, interaction: discord.Interaction):
        model, thinking = get_model_config()
        thinking_label = "ON" if thinking else "OFF"

        desc = (
            f"**モデル:** {model}\n"
            f"**Thinking:** {thinking_label}\n"
            f"**Claude実行中:** {'はい' if self.bot.running_tasks else 'いいえ'}"
        )
        await interaction.response.send_message(
            embed=make_info_embed("ステータス", desc), ephemeral=True
        )

    @app_commands.command(name="cancel", description="実行中の Claude Code タスクをキャンセルする")
    async def cancel_command(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        task = self.bot.running_tasks.get(channel_id)
        if task and not task.done():
            proc = self.bot.running_processes.get(channel_id)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            task.cancel()
            await interaction.response.send_message(
                embed=make_info_embed("キャンセル", "タスクをキャンセルしました。"), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=make_info_embed("キャンセル", "実行中のタスクはありません。"), ephemeral=True
            )

    @app_commands.command(name="reset", description="会話セッションをリセットする")
    async def reset_command(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        task = self.bot.running_tasks.get(channel_id)
        if task and not task.done():
            proc = self.bot.running_processes.get(channel_id)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            task.cancel()
        deleted = delete_channel_session(channel_id)
        if deleted:
            await interaction.response.send_message(
                embed=make_info_embed("リセット", "セッションをリセットしました。次のメッセージから新しい会話が始まります。"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=make_info_embed("リセット", "このチャンネルにはアクティブなセッションがありません。"),
                ephemeral=True,
            )

    @app_commands.command(name="mention", description="このチャンネルでのメンション要否を設定する")
    async def mention_command(self, interaction: discord.Interaction):
        current = str(interaction.channel_id) in get_no_mention_channels()
        view = MentionView(interaction.channel_id, current)
        await interaction.response.send_message(
            embed=MentionView.make_embed(current), view=view, ephemeral=True
        )

    @app_commands.command(name="skills-list", description="利用可能なスキル一覧を表示し、選択で発動する")
    async def skills_list_command(self, interaction: discord.Interaction):
        import asyncio
        from core.config import BASE_DIR
        await asyncio.to_thread(self.bot.skill_registry.reload, BASE_DIR / "skills")
        skills = [s for s in self.bot.skill_registry.all_skills() if s.user_invocable]

        errors = self.bot.skill_registry.load_errors

        if not skills and not errors:
            await interaction.response.send_message(
                embed=make_info_embed("スキル一覧", "利用可能なスキルがありません。"), ephemeral=True
            )
            return

        embed = discord.Embed(title="利用可能なスキル", color=0x5865F2)
        if skills:
            for skill in skills[:25]:
                desc = skill.description[:100] + "…" if len(skill.description) > 100 else skill.description
                embed.add_field(name=skill.name, value=desc, inline=False)
        else:
            embed.description = "利用可能なスキルはありません。"

        if errors:
            err_text = ""
            for p, err in errors:
                err_text += f"• `{p.parent.name}`: {err}\n"
            embed.add_field(name="⚠️ 読み込みエラーのスキル", value=err_text[:1024], inline=False)

        view = SkillsListView(self.bot, skills) if skills else None
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(UtilityCog(bot))
