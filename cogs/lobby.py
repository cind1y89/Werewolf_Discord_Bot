from __future__ import annotations


import discord
from discord import app_commands
from discord.ext import commands

from game import config
from game.models import CHOICE_RULES, FACTION_NAMES, Game, preset_blurb, rules_text
from game.night import unsupported_roles
from game.loop import start_game
from game.room import check_permissions, setup_room, teardown_room
from game.setup import deal_roles, default_roles, describe_roles


# ---------------------------------------------------------------- after the deal
class StartView(discord.ui.View):
    """Under the lobby message after the deal: the host's button that starts the first night.
    Players need no button to learn their character, it is posted in their own channel."""

    def __init__(self, game: Game, show_start: bool = True):
        super().__init__(timeout=None)
        self.game = game
        if not show_start:
            self.remove_item(self.start_night_button)
        if not any(p.is_fake for p in game.players.values()):  # the all-roles peek is for test games with bots
            self.remove_item(self.debug_button)

    @discord.ui.button(label="天黑請閉眼", style=discord.ButtonStyle.primary, emoji="🌙")
    async def start_night_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("只有房主可以開始夜晚。", ephemeral=True)
            return
        remaining = StartView(self.game, show_start=False)
        await interaction.response.edit_message(view=remaining if remaining.children else None)
        start_game(self.game)

    @discord.ui.button(label="全部身分（測試）", style=discord.ButtonStyle.secondary, emoji="🔍")
    async def debug_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("只有房主可以。", ephemeral=True)
            return
        lines = [
            f"{p.seat}號 {p.member.display_name}：{self.game.role_of(p)['name']}"
            f"{'' if p.state['is_alive'] else '（已死亡）'}"
            for p in sorted(self.game.players.values(), key=lambda p: p.seat)
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------- lobby
class LobbyView(discord.ui.View):
    def __init__(self, game: Game, games: dict[int, Game]):
        super().__init__(timeout=900)
        self.game = game
        self.games = games
        self.message: discord.Message | None = None

    def get_embed(self) -> discord.Embed:
        g = self.game
        names = "\n".join(f"• {p.member.display_name}" for p in g.players.values())
        ok, count_line = g.count_check()
        mode = f"預設：{g.data['presets'][g.preset_id]['name']}" if g.config_mode == "preset" else "自訂"
        return discord.Embed(
            title="🐺 狼人殺開局報名中",
            description=(
                f"房主：{g.host.mention}\n"
                f"{count_line}\n"
                f"角色配置（{mode}）：{g.role_summary()}\n"
                f"遊戲模式：{g.victory_summary()}\n"
                f"時間：夜晚 {g.night_seconds} 秒｜發言 {g.speak_text}\n"
                f"警長競選：{'開' if g.sheriff_enabled else '關'}\n\n**玩家名單：**\n{names}"
            ),
            color=discord.Color.dark_red(),
        )

    async def on_timeout(self):
        if self.game.is_started:
            return
        self.games.pop(self.game.channel_id, None)
        if self.message:
            embed = discord.Embed(title="⌛ 報名已逾時", description="請重新使用 /werewolf 開局。")
            await self.message.edit(embed=embed, view=None)

    @discord.ui.button(label="加入遊戲", style=discord.ButtonStyle.success, emoji="✋")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if g.is_started:
            await interaction.response.send_message("遊戲已經開始了！", ephemeral=True)
        elif interaction.user.id in g.players:
            await interaction.response.send_message("你已經在名單裡了！", ephemeral=True)
        else:
            g.add_player(interaction.user)
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="加假人（測試）", style=discord.ButtonStyle.secondary, emoji="🤖")
    async def fake_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("只有房主可以。", ephemeral=True)
        elif self.game.is_started:
            await interaction.response.send_message("遊戲已經開始了！", ephemeral=True)
        else:
            self.game.add_fake_player()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="設定遊戲", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def config_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("只有房主可以設定！", ephemeral=True)
        elif self.game.is_started:
            await interaction.response.send_message("遊戲已經開始了！", ephemeral=True)
        else:
            await ConfigPanel(self.game, self).open(interaction)

    @discord.ui.button(label="開始發牌", style=discord.ButtonStyle.primary, emoji="🚀")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("只有房主可以按開始！", ephemeral=True)
        elif self.game.is_started:
            await interaction.response.send_message("遊戲已經開始了！", ephemeral=True)
        else:
            confirm = ConfirmView(self.game, self)
            await interaction.response.send_message(
                embed=confirm.get_embed(), view=confirm, ephemeral=True
            )


# ---------------------------------------------------------------- confirm + deal
class ConfirmView(discord.ui.View):
    def __init__(self, game: Game, lobby: LobbyView):
        super().__init__(timeout=300)
        self.game = game
        self.lobby = lobby
        self.busy = False
        self.build()

    def build(self) -> None:
        self.confirm_button.disabled = bool(self.game.problems())

    def get_embed(self, extra: list[str] | None = None) -> discord.Embed:
        g = self.game
        problems = g.problems() + (extra or [])
        roles = "\n".join(
            f"• {g.data['roles'][rid]['name']} ×{g.role_list.count(rid)}"
            f"（{FACTION_NAMES.get(g.data['roles'][rid]['initial_state']['faction'], '')}）"
            for rid in dict.fromkeys(g.role_list)
        ) or "（空）"
        embed = discord.Embed(
            title="📋 確認遊戲設定",
            color=discord.Color.red() if problems else discord.Color.green(),
        )
        embed.add_field(name="玩家人數", value=str(len(g.players)))
        embed.add_field(name="角色數量", value=str(len(g.role_list)))
        embed.add_field(name="角色配置", value=(
            f"預設：{g.data['presets'][g.preset_id]['name']}" if g.config_mode == "preset" else "自訂"))
        embed.add_field(name="參加的玩家", value=g.attendees_text()[:1000], inline=False)
        embed.add_field(name="角色", value=roles, inline=False)
        if g.count_check()[0]:
            embed.add_field(name="人數檢查", value=g.count_check()[1], inline=False)
        embed.add_field(name="遊戲模式", value=g.victory_summary(), inline=False)
        embed.add_field(name="女巫自救", value=g.witch_rule, inline=False)
        if g.rules_changed():
            embed.add_field(name="規則調整", value=g.rules_changed()[:1000], inline=False)
        if g.board_notes():
            embed.add_field(name="板型說明", value="\n".join(g.board_notes()), inline=False)
        embed.add_field(
            name="時間",
            value=f"夜晚 {g.night_seconds} 秒（狼人 {g.wolf_seconds} 秒）｜發言 {g.speak_text}｜"
                  f"座位暱稱 {'開' if g.seat_nicks else '關'}｜警長競選 {'開' if g.sheriff_enabled else '關'}", inline=False,
        )
        unsupported = unsupported_roles(g)
        if unsupported:
            embed.add_field(
                name="提醒", value="以下角色的技能尚未實作，會被發牌但不會行動：" + "、".join(unsupported),
                inline=False,
            )
        if problems:
            embed.add_field(name="⚠️ 無法開始", value="\n".join(problems), inline=False)
        else:
            embed.set_footer(text="確認後將隨機分配座位與身分，並建立每位玩家的專屬頻道（只有本人看得到），身分會直接貼在裡面。")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.host.id:
            await interaction.response.send_message("只有房主可以確認。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="確認開始", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if self.busy or g.is_started:
            await interaction.response.defer()
            return
        self.busy = True
        await interaction.response.defer()
        try:
            if g.problems():  # settings may have changed since this screen opened
                self.build()
                await interaction.edit_original_response(embed=self.get_embed(), view=self)
                return

            missing = check_permissions(g) if g.guild else []
            if missing:
                self.build()
                await interaction.edit_original_response(
                    embed=self.get_embed(["Bot 權限不足：" + "；".join(missing)]), view=self
                )
                return

            deal_roles(g)
            try:
                failed_nicks = await setup_room(g)
            except Exception as exc:  # roll everything back so the host can retry
                await teardown_room(g)
                g.undeal()
                self.build()
                await interaction.edit_original_response(
                    embed=self.get_embed([f"建立遊戲房間失敗：{exc}"]), view=self
                )
                return

            self.lobby.stop()
            roster = "🪑 **座位表**\n" + g.roster_text() + "\n\n🎴 **本輪角色配置**\n" + g.config_text() + "\n\n🎖 " + g.sheriff_text()
            if failed_nicks:
                roster += "\n\n（無法修改暱稱：" + "、".join(failed_nicks) + "）"
            await g.channel.send(roster, allowed_mentions=discord.AllowedMentions.none())
            if self.lobby.message:
                embed = discord.Embed(
                    title="🌙 天黑請閉眼 — 發牌完成！",
                    description=("座位與身分已分配。每位玩家的身分已貼在自己的專屬頻道（seat-座位號）裡，只有本人看得到。\n"
                                 "夜晚時大家會被移到各自的頻道，白天回到主語音頻道。確認都看過後，由房主按「天黑請閉眼」。"),
                    color=discord.Color.dark_blue(),
                )
                await self.lobby.message.edit(embed=embed, view=StartView(g))
                g.lobby_message = self.lobby.message
            done = "✅ 已發牌。"
            if g.wolf_leaks:
                done += (
                    "\n⚠️ 以下玩家不是狼人，但看得到狼人語音頻道："
                    + "、".join(g.wolf_leaks)
                    + "\n（伺服器擁有者與管理員無法被隱藏，測試請用一般帳號。）"
                )
            await interaction.edit_original_response(content=done, embed=None, view=None)
            self.stop()
        finally:
            self.busy = False

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="已取消。", embed=None, view=None)
        self.stop()


# ---------------------------------------------------------------- config panel
FACTION_EMOJI = {"VILLAGER": "😇", "WEREWOLF": "🐺", "THIRD_PARTY": "🎭"}

class RoleButton(discord.ui.Button):
    def __init__(self, role_id: str, name: str, count: int, row: int, disabled: bool):
        super().__init__(
            label=f"{name} ×{count}" if count else name,
            style=discord.ButtonStyle.success if count else discord.ButtonStyle.secondary,
            row=row,
            disabled=disabled,
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        panel: ConfigPanel = self.view.panel
        await interaction.response.defer()
        roles = panel.game.role_list
        if panel.removing:
            if self.role_id in roles:
                roles.remove(self.role_id)
        else:
            roles.append(self.role_id)
        await panel.sync()


class PanelView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=840)
        self.panel = panel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.panel.game.host.id or self.panel.game.is_started:
            await interaction.response.send_message("無法修改設定。", ephemeral=True)
            return False
        return True


class RolesView(PanelView):
    """One section (faction) of role buttons. Discord allows 25 buttons per message."""

    def __init__(self, panel: ConfigPanel, faction: str):
        super().__init__(panel)
        self.faction = faction

    def header(self) -> str:
        emoji = FACTION_EMOJI.get(self.faction, "🎭")
        return f"{emoji} **{FACTION_NAMES.get(self.faction, self.faction)}**（綠色＝已選）"

    def build(self) -> None:
        self.clear_items()
        game = self.panel.game
        locked = game.config_mode == "preset"
        roles = [
            (rid, r) for rid, r in game.data["roles"].items()
            if r["initial_state"]["faction"] == self.faction
        ]
        for i, (rid, r) in enumerate(roles[:25]):
            self.add_item(RoleButton(rid, r["name"], game.role_list.count(rid), i // 5, locked))


CUSTOM = "__custom__"


class ConfigSelect(discord.ui.Select):
    """角色配置: a preset (fixed roles) or 自訂 (pick the roles by hand)."""

    def __init__(self, game: Game):
        custom = game.config_mode == "custom"
        presets = game.data["presets"]
        if not custom and game.preset_id not in presets:  # keep the chosen board visible
            presets = {**presets, game.preset_id: game.data["presets"][game.preset_id]}
        options = [
            discord.SelectOption(
                label=f"角色配置：{p['name']}"[:100], value=pid, default=(not custom and pid == game.preset_id),
                description=preset_blurb(p) or None,
            )
            for pid, p in list(presets.items())[:24]
        ]
        options.append(discord.SelectOption(
            label="角色配置：自訂", value=CUSTOM, default=custom,
            description="自己選角色：用上方的角色按鈕與 新增/移除/清空",
        ))
        super().__init__(placeholder="角色配置（點此更改）", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        panel: ConfigPanel = self.view.panel
        await interaction.response.defer()
        game = panel.game
        if self.values[0] == CUSTOM:
            game.config_mode = "custom"  # keeps the roles you have now as the starting point
            game.rule_overrides = {}  # board rules belong to the board; a custom set uses the defaults
        else:
            game.config_mode = "preset"
            game.set_preset(self.values[0])
        await panel.sync()


class GameModeSelect(discord.ui.Select):
    """遊戲模式: how the wolves win (屠邊 or 屠城)."""

    def __init__(self, game: Game):
        options = [
            discord.SelectOption(
                label=f"遊戲模式：{m['name']}", value=mid, default=(mid == game.victory_mode),
                description=m["description"][:100],
            )
            for mid, m in game.data["victory_modes"]["modes"].items()
        ]
        super().__init__(placeholder="遊戲模式（點此更改）", options=options, row=4)

    async def callback(self, interaction: discord.Interaction):
        panel: ConfigPanel = self.view.panel
        await interaction.response.defer()
        panel.game.victory_mode = self.values[0]
        await panel.sync()


class TimeSelect(discord.ui.Select):
    """Pick a time from a list. `attr` is the Game field it sets; 0 means unlimited.
    Each choice reads '<name>：<value>', so the closed list always says what it is and what is set."""

    def __init__(self, game: Game, attr: str, title: str, hint: str, choices, row: int):
        current = getattr(game, attr)
        options = [
            discord.SelectOption(
                label=f"{title}：{c} 秒" if c else f"{title}：不限時", value=str(c),
                default=(c == current), description=hint,
            )
            for c in choices
        ]
        super().__init__(placeholder=f"{title}（點此更改）", options=options, row=row)
        self.attr = attr

    async def callback(self, interaction: discord.Interaction):
        panel: ConfigPanel = self.view.panel
        await interaction.response.defer()
        setattr(panel.game, self.attr, int(self.values[0]))
        await panel.sync()


class RuleSelect(discord.ui.Select):
    """A rule with a few choices (a number): the closed list says what it is and what is set."""

    def __init__(self, game: Game, key: str, row: int):
        title, choices = CHOICE_RULES[key]
        current = {**game.data["rule_set"]["settings"], **game.rule_overrides}[key]
        if key == "sheriff_swallow_booms":
            current = game.swallow_booms()  # "by player count" shows as what it comes to
        super().__init__(placeholder=f"{title}（點此更改）", row=row, options=[
            discord.SelectOption(label=f"{title}：{label}"[:100], value=str(i), default=(value == current))
            for i, (value, label) in enumerate(choices)])
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.panel.game.rule_overrides[self.key] = CHOICE_RULES[self.key][1][int(self.values[0])][0]
        await self.view.panel.sync()


class RulesView(PanelView):
    """The few rules the host decides. The board's own rules are only the starting point.
    One class, two messages: `part` 'night' (wolves' time, witch self-heal) or 'day' (ties, the badge)."""

    def __init__(self, panel: "ConfigPanel", part: str):
        super().__init__(panel)
        self.part = part

    def build(self) -> None:
        self.clear_items()
        g = self.panel.game
        if self.part == "night":
            self.add_item(TimeSelect(g, "wolf_seconds", "狼人夜晚時間", "狼人討論並選擇襲擊目標的時間",
                                     config.NIGHT_WOLF_SECONDS_CHOICES, row=0))
            settings = {**g.data["rule_set"]["settings"], **g.rule_overrides}
            for key, label in (("witch_first_night_self_heal", "女巫第一夜自救"),
                               ("witch_later_nights_self_heal", "女巫第二夜起自救")):
                on = settings[key]
                button = discord.ui.Button(
                    label=f"{label}：{'可以' if on else '不可以'}", row=1,
                    style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary)
                button.callback = self._toggle(key, not on)
                self.add_item(button)
        else:
            self.add_item(RuleSelect(g, "sheriff_swallow_booms", 0))
            self.add_item(RuleSelect(g, "tie_draw_days", 1))

    def _toggle(self, key: str, value: bool):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            self.panel.game.rule_overrides[key] = value
            await self.panel.sync()
        return callback


class ControlsView(PanelView):
    """Rows: 0 add/remove/clear + seat nicknames + 警長競選, 1 角色配置 list, 2 夜晚時間 list, 3 發言時間 list,
    4 遊戲模式 list."""

    def _add(self, label, style, row, fn, disabled=False):
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)

        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            fn()
            await self.panel.sync()

        button.callback = callback
        self.add_item(button)

    def build(self) -> None:
        self.clear_items()
        panel, g = self.panel, self.panel.game
        off = discord.ButtonStyle.secondary
        custom = g.config_mode == "custom"

        def set_removing(value):
            def fn():
                panel.removing = value
            return fn

        # row 0: how a click on a role button works (custom only) + the nickname switch
        self._add("新增 ➕", discord.ButtonStyle.success if not panel.removing else off,
                  0, set_removing(False), disabled=not custom)
        self._add("移除 ➖", discord.ButtonStyle.danger if panel.removing else off,
                  0, set_removing(True), disabled=not custom)
        self._add("清空", off, 0, g.role_list.clear, disabled=not custom)

        def toggle_nicks():
            g.seat_nicks = not g.seat_nicks

        self._add(f"座位暱稱：{'開' if g.seat_nicks else '關'}",
                  discord.ButtonStyle.success if g.seat_nicks else off, 0, toggle_nicks)

        def toggle_sheriff():
            g.sheriff_enabled = not g.sheriff_enabled

        self._add(f"警長競選：{'開' if g.sheriff_enabled else '關'}",
                  discord.ButtonStyle.success if g.sheriff_enabled else off, 0, toggle_sheriff)

        # rows 1-4: four lists, each says what it is and shows what is set
        self.add_item(ConfigSelect(g))
        self.add_item(TimeSelect(g, "night_seconds", "夜晚行動時間", "每個角色的行動時間（狼人另外設定）",
                                 config.NIGHT_SECONDS_CHOICES, row=2))
        self.add_item(TimeSelect(g, "speak_seconds", "白天發言時間", "白天每位玩家的發言時間（不限時要按跳過結束）",
                                 config.SPEAK_SECONDS_CHOICES, row=3))
        self.add_item(GameModeSelect(g))


class ConfigPanel:
    """Host-only setup as private messages: one role section per faction, then the controls."""

    def __init__(self, game: Game, lobby: LobbyView):
        self.game = game
        self.lobby = lobby
        self.removing = False
        factions_in_data = {r["initial_state"]["faction"] for r in game.data["roles"].values()}
        order = [f for f in FACTION_NAMES if f in factions_in_data]
        order += sorted(factions_in_data - set(order))
        self.sections = [RolesView(self, f) for f in order]
        self.rule_views = [RulesView(self, "night"), RulesView(self, "day")]
        self.rule_msgs: list[discord.WebhookMessage] = []
        self.controls_view = ControlsView(self)
        self.origin: discord.Interaction | None = None
        self.section_msgs: list[discord.WebhookMessage] = []
        self.controls_msg: discord.WebhookMessage | None = None

    def get_embed(self) -> discord.Embed:
        g = self.game
        mode = f"預設：{g.data['presets'][g.preset_id]['name']}（角色固定，要手動增減請在「角色配置」選「自訂」）" \
            if g.config_mode == "preset" else (
                f"自訂（點角色按鈕＝{'移除' if self.removing else '新增'}一個）")
        text = (
            f"**角色配置：**{mode}\n"
            f"**角色：**{g.role_summary()}\n"
            f"**遊戲模式：**{g.victory_summary()}\n"
            f"**女巫自救：**{g.witch_rule}\n"
            + (f"**規則調整：**{g.rules_changed()}\n" if g.rules_changed() else "")
            + "".join(f"**板型說明：**{n}\n" for n in g.board_notes())
            + f"**時間：**夜晚每個角色 {g.night_seconds} 秒（狼人 {g.wolf_seconds} 秒），白天每人發言 {g.speak_text}\n"
            f"**座位暱稱：**{'開' if g.seat_nicks else '關'}　**警長競選：**{'開' if g.sheriff_enabled else '關'}\n\n"
            f"**參加的玩家：**{g.attendees_text()}\n"
            f"{g.count_check()[1]}"
        )
        problems = g.problems()
        if problems:
            text += "\n⚠️ " + "\n⚠️ ".join(problems)
        return discord.Embed(title="⚙️ 遊戲設定", description=text, color=discord.Color.gold())

    def _build_all(self):
        for view in self.sections:
            view.build()
        for view in self.rule_views:
            view.build()
        self.controls_view.build()

    async def open(self, interaction: discord.Interaction):
        self.origin = interaction
        self._build_all()
        first, *rest = self.sections
        await interaction.response.send_message(first.header(), view=first, ephemeral=True)
        for view in rest:
            self.section_msgs.append(
                await interaction.followup.send(view.header(), view=view, ephemeral=True, wait=True)
            )
        for view, title in zip(self.rule_views, ("🌙 夜晚規則（狼人時間、女巫自救）", "☀ 警長與平票規則（吞警徽、連續平票）")):
            self.rule_msgs.append(await interaction.followup.send(title, view=view, ephemeral=True, wait=True))
        self.controls_msg = await interaction.followup.send(
            embed=self.get_embed(), view=self.controls_view, ephemeral=True, wait=True
        )

    async def sync(self):
        self._build_all()
        await self.origin.edit_original_response(view=self.sections[0])
        for msg, view in zip(self.section_msgs, self.sections[1:]):
            await msg.edit(view=view)
        for msg, view in zip(self.rule_msgs, self.rule_views):
            await msg.edit(view=view)
        await self.controls_msg.edit(embed=self.get_embed(), view=self.controls_view)
        if self.lobby.message:
            await self.lobby.message.edit(embed=self.lobby.get_embed())


# ---------------------------------------------------------------- commands
# ---------------------------------------------------------------- the start menu
class BoardSelect(discord.ui.Select):
    def __init__(self, data: dict):
        options = [
            discord.SelectOption(label=p["name"][:100], value=pid, description=preset_blurb(p) or None)
            for pid, p in list(data["presets"].items())[:24]
        ]
        options.append(discord.SelectOption(
            label="自訂配置", value=CUSTOM, description="自己選角色（先給一組基本配置，之後可以改）"))
        super().__init__(placeholder="選擇板型（人數以報名的玩家為準）", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.view.chose_board(interaction, self.values[0])


class StartWizard(discord.ui.View):
    """/werewolf: pick a board (or a custom set). Host only. The number of players is whoever joins."""

    def __init__(self, cog: "Lobby", host: discord.abc.User):
        super().__init__(timeout=180)
        self.cog, self.host = cog, host
        self.data = cog.bot.game_data
        self.add_item(BoardSelect(self.data))

    def boards_embed(self, note: str = "") -> discord.Embed:
        lines = []
        for pid, p in self.data["presets"].items():
            extras = [t for t in (rules_text(p.get("rules", {})), p.get("note")) if t]
            lines.append(f"**{p['name']}**\n{describe_roles(self.data, p['roles'])}"
                         + (f"\n_{'；'.join(extras)}_" if extras else ""))
        text = (note + "\n\n" if note else "") + "選一個板型，再讓玩家報名。**報名的人數必須等於角色數量**，不相符就不能開始。\n\n"
        text += "\n\n".join(lines)
        return discord.Embed(title="🐺 開一局狼人殺", description=text[:4000], color=discord.Color.dark_red())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("這是開局者的選單。", ephemeral=True)
            return False
        return True

    async def chose_board(self, interaction: discord.Interaction, board: str) -> None:
        error = await self.cog.open_lobby(interaction, board)
        if error:
            await interaction.response.edit_message(embed=self.boards_embed(f"⚠️ {error}"), view=self)
            return
        self.stop()
        await interaction.response.edit_message(
            content="✅ 已開啟，請到頻道裡的報名訊息操作（⚙️ 設定遊戲 可以再改板型與設定）。", embed=None, view=None)


class Lobby(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, Game] = {}  # {channel_id: Game}

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        """Server mute is cleared when someone leaves voice, so a player who reconnects during the
        game gets the right state straight away (muted in the day unless it is their turn)."""
        if after.channel is None or before.channel == after.channel:
            return
        game = next((g for g in self.games.values()
                     if g.room_ready and not g.is_over and member.id in g.players
                     and g.guild and g.guild.id == member.guild.id), None)
        if game is None:
            return
        want = game.desired_mute(member.id)
        if want is None:
            return
        if after.mute == want:
            game.voice_applied[member.id] = want
            return
        try:
            await member.edit(mute=want, reason="狼人殺發言控制")
            game.voice_applied[member.id] = want
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def open_lobby(self, interaction: discord.Interaction, board: str) -> str | None:
        """Create the game and post the lobby in the channel. `board` is a preset id or CUSTOM.
        Returns an error text, or None if it worked."""
        channel_id = interaction.channel_id
        if channel_id in self.games:
            return "這個頻道已經有遊戲了！請先使用 /reset 重置。"
        data = self.bot.game_data
        game = Game(interaction.user, channel_id, data)
        game.guild, game.channel = interaction.guild, interaction.channel
        game.client = interaction.client
        game.voice_channel = getattr(interaction.user.voice, "channel", None)  # voice is used if the host is in one
        if board == CUSTOM:
            game.config_mode, game.role_list = "custom", default_roles(6)
        else:
            game.set_preset(board)
        self.games[channel_id] = game
        game.on_end = lambda cid=channel_id: self.games.pop(cid, None)
        view = LobbyView(game, self.games)
        view.message = await interaction.channel.send(embed=view.get_embed(), view=view)
        return None

    @app_commands.command(name="werewolf", description="開啟一局新的狼人殺（選板型，玩家報名）")
    @app_commands.guild_only()
    async def start_werewolf(self, interaction: discord.Interaction):
        if interaction.channel_id in self.games:
            await interaction.response.send_message("這個頻道已經有遊戲了！請先使用 /reset 重置。", ephemeral=True)
            return
        wizard = StartWizard(self, interaction.user)
        await interaction.response.send_message(embed=wizard.boards_embed(), view=wizard, ephemeral=True)

    @app_commands.command(name="reset", description="強制結束並重置目前頻道的遊戲")
    async def reset_werewolf(self, interaction: discord.Interaction):
        game = self.games.pop(interaction.channel_id, None)
        if game:
            await interaction.response.defer()
            if game.task:
                game.task.cancel()
            await teardown_room(game)
            await interaction.followup.send("🔄 已重置本頻道的狼人殺房間，並還原暱稱、頻道與語音權限。")
        else:
            await interaction.response.send_message("目前沒有進行中的遊戲。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))
