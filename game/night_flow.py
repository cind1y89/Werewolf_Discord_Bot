"""The night as a Discord flow: public prompts, private action panels, wolf vote."""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta

import discord

from . import config
from . import night as N
from .models import Game, Player
from .room import isolate_players, move_wolves, refresh_cards, set_voice
from .sound import beep


def opt_label(p: Player) -> str:
    return f"{p.seat}號 {p.member.display_name}"[:100]


async def say(game: Game, text: str, **kwargs) -> discord.Message:
    return await game.channel.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)


def countdown(seconds: int) -> str:
    """A Discord timestamp that counts down on every player's screen, e.g. 'in 45 seconds'."""
    if not seconds:
        return "不限時"
    return discord.utils.format_dt(discord.utils.utcnow() + timedelta(seconds=seconds), "R")


def start_warnings(seconds: float | None, send):
    """Post a reminder when 30 and when 10 seconds are left (config.WARN_SECONDS), so nobody depends on the
    countdown clock of their own device. `send(left)` is an async function that posts it. Returns the task
    (cancel it with stop_warnings when the wait ends early), or None when the time is too short or unlimited."""
    marks = sorted((m for m in config.WARN_SECONDS if seconds and seconds > m), reverse=True)
    if not marks:
        return None

    async def run() -> None:
        waited = 0.0
        for mark in marks:
            await asyncio.sleep(seconds - mark - waited)
            waited = seconds - mark
            try:
                await send(mark)
            except (discord.HTTPException, AttributeError):
                pass

    return asyncio.create_task(run())


def stop_warnings(task) -> None:
    if task:
        task.cancel()


def ping(players) -> str:
    """Mentions of the real players, so Discord notifies them. Bots just show a name."""
    return " ".join(p.member.mention for p in players if not p.is_fake)


async def lock_voice(game: Game, allowed: set[int]) -> None:
    """Server-mute everyone except `allowed`; say so once if the bot is not allowed to."""
    await set_voice(game, allowed)
    if game.voice_failed and not game.voice_failed_warned:
        game.voice_failed_warned = True
        await say(game, "⚠️ Bot 無法靜音玩家，發言輪替不會生效。請給 Bot「禁言成員」(Mute Members) 與「移動成員」權限。")


# ---------------------------------------------------------------- prompt bookkeeping
class Prompt:
    """One turn in progress: who still has to act, and when it is over."""

    def __init__(self, game: Game, turn: N.Turn, state: N.NightState, allow_dead: bool = False):
        self.game, self.turn, self.state = game, turn, state
        self.actors = [a for a in turn.actors if (allow_dead or a.state["is_alive"]) and a.id not in state.feared]
        if turn.key == "wolves" and N.wolves_blocked(game, state):
            self.actors = []  # a feared wolf: the pack cannot attack tonight
        self.pending = {a.id for a in self.actors if not a.is_fake}
        self.event = asyncio.Event()
        self.closed = False
        self.on_record = None  # async callback after someone records a choice (wolf vote board)
        if not self.pending:
            self.event.set()

    def complete(self, actor_id: int) -> None:
        self.pending.discard(actor_id)
        if not self.pending:
            self.event.set()

    def actor_for(self, user_id: int) -> Player | None:
        return next((a for a in self.actors if a.id == user_id), None)


# ---------------------------------------------------------------- private action panels
class PickSelect(discord.ui.Select):
    def __init__(self, players: list[Player], placeholder: str, selected: int | None, row: int = 0):
        super().__init__(
            placeholder=placeholder, row=row,
            options=[
                discord.SelectOption(label=opt_label(p), value=str(p.id), default=(p.id == selected))
                for p in players[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.select(interaction, int(self.values[0]))


class OwnedView(discord.ui.View):
    """A private panel that only its owner may press."""

    def __init__(self, owner: Player, timeout: float):
        super().__init__(timeout=timeout)
        self.owner = owner

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("這不是你的面板。", ephemeral=True)
            return False
        return True


class ConfirmStep(OwnedView):
    """'Are you sure?' with 確認 and 返回."""

    def __init__(self, owner: Player, on_confirm, on_back):
        super().__init__(owner, config.PANEL_TIMEOUT)
        self.on_confirm, self.on_back = on_confirm, on_back

    @discord.ui.button(label="確認", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_confirm(interaction)

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_back(interaction)


class ActionPanel(OwnedView):
    """Pick one player, then press 確認. `describe(target_id)` gives the confirmation text
    and `side_effect(target_id)` runs bookkeeping such as using up a potion.
    The last button is 'don't act' (records nothing), or with `back` a way back.
    With skip_label=None the action is mandatory and there is no such button."""

    def __init__(self, prompt: Prompt, actor: Player, ability: str, options: list[Player],
                 header: str, placeholder: str, describe, side_effect=None,
                 skip_label: str | None = "不行動", skip_text: str = "你選擇不行動。", back=None):
        super().__init__(actor, config.PANEL_TIMEOUT)
        self.prompt, self.actor, self.ability = prompt, actor, ability
        self.options, self.header, self.placeholder = options, header, placeholder
        self.describe, self.side_effect, self.back = describe, side_effect, back
        self.skip_label, self.skip_text = skip_label, skip_text
        self.selected: int | None = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        if self.options:
            self.add_item(PickSelect(self.options, self.placeholder, self.selected))
        confirm = discord.ui.Button(
            label="確認", style=discord.ButtonStyle.success, emoji="✅",
            disabled=self.selected is None, row=1,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)
        if self.skip_label:
            skip = discord.ui.Button(label=self.skip_label, style=discord.ButtonStyle.secondary, row=1)
            skip.callback = self._skip
            self.add_item(skip)

    async def select(self, interaction: discord.Interaction, target_id: int):
        self.selected = target_id
        self._build()
        label = N.label(self.prompt.game, target_id)
        await interaction.response.edit_message(
            content=f"{self.header}\n已選擇：**{label}**。確認無誤請按「確認」，也可以重選。", view=self
        )

    def _open(self) -> bool:
        return not self.prompt.closed and self.actor.id in self.prompt.pending

    async def _confirm(self, interaction: discord.Interaction):
        if not self._open():
            await interaction.response.edit_message(content="這個回合已結束。", view=None)
            return
        target = self.selected
        self.prompt.state.record(self.ability, self.actor.id, target)
        if self.side_effect:
            self.side_effect(target)
        self.prompt.complete(self.actor.id)
        await interaction.response.edit_message(content=self.describe(target), view=None)
        if self.prompt.on_record:
            await self.prompt.on_record()

    async def _skip(self, interaction: discord.Interaction):
        if self.back:
            text, view = self.back()
            await interaction.response.edit_message(content=text, view=view)
            return
        if not self._open():
            await interaction.response.edit_message(content="這個回合已結束。", view=None)
            return
        self.prompt.state.record(self.ability, self.actor.id, None)
        self.prompt.complete(self.actor.id)
        await interaction.response.edit_message(content=self.skip_text, view=None)
        if self.prompt.on_record:
            await self.prompt.on_record()


class PairSelect(discord.ui.Select):
    def __init__(self, players: list[Player], placeholder: str):
        super().__init__(
            placeholder=placeholder, min_values=2, max_values=2,
            options=[discord.SelectOption(label=opt_label(p), value=str(p.id)) for p in players[:25]],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.select_pair(interaction, [int(v) for v in self.values])


class PairPanel(OwnedView):
    """Pick exactly two players (cupid's lovers, the magician's swap), then 確認."""

    def __init__(self, prompt: Prompt, actor: Player, store_key: str, options: list[Player],
                 header: str, describe, skip_label: str | None = "不行動"):
        super().__init__(actor, config.PANEL_TIMEOUT)
        self.prompt, self.actor, self.store_key = prompt, actor, store_key
        self.header, self.describe, self.pair = header, describe, None
        self.add_item(PairSelect(options, "選擇兩名玩家"))
        self.confirm_btn = discord.ui.Button(label="確認", style=discord.ButtonStyle.success, emoji="✅",
                                             disabled=True, row=1)
        self.confirm_btn.callback = self._confirm
        self.add_item(self.confirm_btn)
        if skip_label:
            skip = discord.ui.Button(label=skip_label, style=discord.ButtonStyle.secondary, row=1)
            skip.callback = self._skip
            self.add_item(skip)

    async def select_pair(self, interaction: discord.Interaction, pair: list[int]):
        self.pair = pair
        self.confirm_btn.disabled = False
        names = "、".join(N.label(self.prompt.game, i) for i in pair)
        await interaction.response.edit_message(
            content=f"{self.header}\n已選擇：**{names}**。確認無誤請按「確認」。", view=self
        )

    def _open(self) -> bool:
        return not self.prompt.closed and self.actor.id in self.prompt.pending

    async def _confirm(self, interaction: discord.Interaction):
        if not self._open():
            await interaction.response.edit_message(content="這個回合已結束。", view=None)
            return
        self.prompt.state.extra.setdefault(self.store_key, {})[self.actor.id] = self.pair
        self.prompt.complete(self.actor.id)
        await interaction.response.edit_message(content=self.describe(self.pair), view=None)

    async def _skip(self, interaction: discord.Interaction):
        if self._open():
            self.prompt.complete(self.actor.id)
        await interaction.response.edit_message(content="你選擇不行動。", view=None)


class InfoPanel(OwnedView):
    """Shows information only, with 知道了."""

    def __init__(self, prompt: Prompt, actor: Player):
        super().__init__(actor, config.PANEL_TIMEOUT)
        self.prompt, self.actor = prompt, actor

    @discord.ui.button(label="知道了", style=discord.ButtonStyle.primary, emoji="👌")
    async def ok(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.prompt.complete(self.actor.id)
        await interaction.response.edit_message(view=None)


class ThiefPanel(OwnedView):
    """Choose one of the two bottom cards."""

    def __init__(self, prompt: Prompt, thief: Player):
        super().__init__(thief, config.PANEL_TIMEOUT)
        self.prompt, self.thief = prompt, thief
        game = prompt.game
        allowed = N.thief_options(game)
        for i, rid in enumerate(game.bottom_cards):
            button = discord.ui.Button(
                label=f"牌{i + 1}：{game.data['roles'][rid]['name']}",
                style=discord.ButtonStyle.primary, disabled=i not in allowed,
            )
            button.callback = self._pick(i, game.data["roles"][rid]["name"])
            self.add_item(button)

    def header(self) -> str:
        forced = any(
            self.prompt.game.data["roles"][r]["initial_state"]["faction"] == "WEREWOLF"
            for r in self.prompt.game.bottom_cards
        )
        return "🦝 **盜賊**：選擇一張底牌作為你本局的身分。" + ("\n底牌中有狼人牌，必須選擇狼人牌。" if forced else "")

    def _pick(self, index: int, name: str):
        async def callback(interaction: discord.Interaction):
            async def confirmed(i: discord.Interaction):
                if self.prompt.closed:
                    await i.response.edit_message(content="這個回合已結束。", view=None)
                    return
                self.prompt.state.extra.setdefault("thief_pick", {})[self.thief.id] = index
                self.prompt.complete(self.thief.id)
                await i.response.edit_message(content=f"你選擇了「{name}」，天亮前你的身分會改變。", view=None)

            async def back(i: discord.Interaction):
                await i.response.edit_message(content=self.header(), view=self)

            await interaction.response.edit_message(
                content=f"確定要選擇「{name}」嗎？", view=ConfirmStep(self.thief, confirmed, back)
            )
        return callback


class GiftSelect(discord.ui.Select):
    def __init__(self, selected: str | None):
        super().__init__(
            placeholder="選擇要給予的技能", row=1,
            options=[discord.SelectOption(label=name, value=key, default=(key == selected))
                     for key, name in N.GIFTS.items()],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.pick_gift(interaction, self.values[0])


class GiftPanel(OwnedView):
    """Miracle merchant: pick the lucky player and the one-time skill, then 確認."""

    def __init__(self, prompt: Prompt, merchant: Player):
        super().__init__(merchant, config.PANEL_TIMEOUT)
        self.prompt, self.merchant = prompt, merchant
        self.target: int | None = None
        self.gift: str | None = None
        self.options = N.options_for(prompt.game, prompt.state, merchant, "grant_lucky_skill")
        self.header = "🎁 **奇蹟商人**：選擇幸運兒與要給予的一次性技能（整局只能用一次）。給到狼人會反噬自己。"
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(PickSelect(self.options, "選擇幸運兒", self.target, row=0))
        self.add_item(GiftSelect(self.gift))
        confirm = discord.ui.Button(label="確認", style=discord.ButtonStyle.success, emoji="✅", row=2,
                                    disabled=self.target is None or self.gift is None)
        confirm.callback = self._confirm
        self.add_item(confirm)
        skip = discord.ui.Button(label="不使用", style=discord.ButtonStyle.secondary, row=2)
        skip.callback = self._skip
        self.add_item(skip)

    def _status(self) -> str:
        game = self.prompt.game
        target = N.label(game, self.target) if self.target is not None else "（未選）"
        gift = N.GIFTS.get(self.gift, "（未選）")
        return f"{self.header}\n幸運兒：**{target}**　技能：**{gift}**"

    async def select(self, interaction: discord.Interaction, target_id: int):
        self.target = target_id
        self._build()
        await interaction.response.edit_message(content=self._status(), view=self)

    async def pick_gift(self, interaction: discord.Interaction, gift: str):
        self.gift = gift
        self._build()
        await interaction.response.edit_message(content=self._status(), view=self)

    async def _confirm(self, interaction: discord.Interaction):
        if self.prompt.closed:
            await interaction.response.edit_message(content="這個回合已結束。", view=None)
            return
        self.prompt.state.record("grant_lucky_skill", self.merchant.id, self.target)
        self.prompt.state.extra.setdefault("gift", {})[self.merchant.id] = self.gift
        self.prompt.complete(self.merchant.id)
        await interaction.response.edit_message(
            content=f"你把「{N.GIFTS[self.gift]}」技能給了 {N.label(self.prompt.game, self.target)}。", view=None
        )

    async def _skip(self, interaction: discord.Interaction):
        self.prompt.complete(self.merchant.id)
        await interaction.response.edit_message(content="你選擇不使用禮物。", view=None)


class WitchPanel(OwnedView):
    def __init__(self, prompt: Prompt, witch: Player):
        super().__init__(witch, config.PANEL_TIMEOUT)
        self.prompt, self.witch = prompt, witch
        self.opts = N.witch_options(prompt.game, witch, prompt.state)
        self.heal.disabled = not self.opts["can_heal"]
        self.poison.disabled = not self.opts["can_poison"]

    def header(self) -> str:
        game, v = self.prompt.game, self.opts["victim"]
        text = "🧪 **女巫**\n" + (
            f"昨晚被襲擊的是 **{N.label(game, v)}**。" if v is not None else "昨晚沒有人被襲擊。"
        )
        text += f"\n解藥 {N.resource_left(self.witch, 'antidote')} 瓶，毒藥 {N.resource_left(self.witch, 'poison')} 瓶。同一晚只能用一瓶。毒藥不能對自己使用。"
        if v == self.witch.id and not self.opts["self_heal_ok"]:
            text += "\n（規則：女巫不能自救。）"
        return text

    def _back(self, interaction: discord.Interaction):
        async def go(i: discord.Interaction):
            await i.response.edit_message(content=self.header(), view=self)
        return go

    @discord.ui.button(label="使用解藥", style=discord.ButtonStyle.success, emoji="💊")
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        victim, game = self.opts["victim"], self.prompt.game

        async def confirmed(i: discord.Interaction):
            if self.prompt.closed:
                await i.response.edit_message(content="這個回合已結束。", view=None)
                return
            N.use_resource(self.witch, "antidote")
            self.prompt.state.record("use_antidote", self.witch.id, victim)
            self.prompt.complete(self.witch.id)
            await i.response.edit_message(content=f"你對 {N.label(game, victim)} 使用了解藥。", view=None)

        step = ConfirmStep(self.witch, confirmed, self._back(interaction))
        await interaction.response.edit_message(
            content=f"確定要對 **{N.label(game, victim)}** 使用解藥嗎？", view=step
        )

    @discord.ui.button(label="使用毒藥", style=discord.ButtonStyle.danger, emoji="☠️")
    async def poison(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.prompt.game
        header = "☠️ 選擇毒殺目標："
        panel = ActionPanel(
            self.prompt, self.witch, "use_poison", N.options_for(game, self.prompt.state, self.witch, "use_poison"),
            header, "選擇要毒殺的玩家（不能毒自己）",
            describe=lambda t: f"你對 {N.label(game, t)} 使用了毒藥。",
            side_effect=lambda t: N.use_resource(self.witch, "poison"),
            skip_label="返回", back=lambda: (self.header(), self),
        )
        await interaction.response.edit_message(content=header, view=panel)

    @discord.ui.button(label="不使用", style=discord.ButtonStyle.secondary)
    async def nothing(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def confirmed(i: discord.Interaction):
            self.prompt.complete(self.witch.id)
            await i.response.edit_message(content="你今晚什麼都沒做。", view=None)

        step = ConfirmStep(self.witch, confirmed, self._back(interaction))
        await interaction.response.edit_message(content="確定今晚不使用任何藥嗎？", view=step)


def _target_panel(prompt: Prompt, actor: Player, ability: str, header: str, placeholder: str,
                  describe, mandatory: bool = False, skip_label: str = "不行動",
                  skip_text: str = "你選擇不行動。", options=None) -> tuple[str, ActionPanel]:
    game, state = prompt.game, prompt.state
    options = options if options is not None else N.options_for(game, state, actor, ability)
    if not options:  # nothing to pick (and a mandatory action has no skip button): say so and let them continue
        return header + "\n\n（目前沒有可以選擇的對象。）", InfoPanel(prompt, actor)
    return header, ActionPanel(
        prompt, actor, ability, options, header, placeholder, describe=describe,
        skip_label=None if mandatory else skip_label, skip_text=skip_text,
    )


def wolf_board_text(prompt: Prompt, final: bool = False) -> str:
    """Who each wolf voted for. Shown only to the wolves."""
    game, votes = prompt.game, prompt.state.wolf_votes
    lines = []
    for wolf in prompt.actors:
        if wolf.id not in votes:
            lines.append(f"{opt_label(wolf)} → （尚未投票）")
        elif votes[wolf.id] is None:
            lines.append(f"{opt_label(wolf)} → 棄權")
        else:
            lines.append(f"{opt_label(wolf)} → {N.label(game, votes[wolf.id])}")
    head = "🐺 **狼人投票**" + ("（已結束）" if final else "（全員投完即結束）")
    return head + "\n" + "\n".join(lines)


def make_panel(prompt: Prompt, actor: Player) -> tuple[str, discord.ui.View]:
    game, state, ability = prompt.game, prompt.state, prompt.turn.ability_id
    real = lambda t: game.players[state.redirect(t)]  # noqa: E731  who a skill really lands on

    def info(text: str) -> tuple[str, discord.ui.View]:
        return text, InfoPanel(prompt, actor)

    # skills sealed by the blood moon apostle (gods only)
    role = game.role_of(actor)
    if state.sealed and role["initial_state"]["category"] == "GOD" and ability not in ("hunter_shot", "day_vote"):
        return info("🩸 你的技能今晚被血月使徒封印，無法使用。")

    if ability == "wolf_pack_attack":
        return _target_panel(
            prompt, actor, ability,
            "🐺 **狼人**：選擇襲擊目標（可在狼人語音頻道討論）。選「棄權」代表不襲擊。\n"
            + wolf_board_text(prompt),
            "選擇襲擊對象",
            lambda t: f"你投票襲擊 {N.label(game, t)}。" if t is not None else "你棄權了。",
            skip_label="棄權", skip_text="你棄權了。", options=N.wolf_options(game, state),
        )
    if ability == "hunter_shot":
        name = role["name"]
        return _target_panel(
            prompt, actor, ability, f"🔫 **{name}**：選擇要帶走的玩家，或選擇不開槍。", "選擇開槍對象",
            lambda t: f"你開槍帶走了 {N.label(game, t)}。", skip_label="不開槍", skip_text="你選擇不開槍。",
            options=[p for p in N.alive(game) if p.id != actor.id],
        )
    if ability == "day_vote":
        from . import day as D
        return _target_panel(
            prompt, actor, ability, "🗳️ **放逐投票**：選擇要放逐的玩家，或棄權。", "選擇放逐對象",
            lambda t: f"你投票放逐 {N.label(game, t)}。", skip_label="棄權", skip_text="你棄權了。",
            options=D.vote_options(game, actor, state.extra.get("vote_candidates")),
        )
    if ability == "sheriff_vote":
        from . import day as D
        return _target_panel(
            prompt, actor, ability, "🎖️ **警長投票**：選擇你支持的警上玩家，或棄權。", "選擇警長人選",
            lambda t: f"你投票給 {N.label(game, t)} 當警長。", skip_label="棄權", skip_text="你棄權了。",
            options=D.vote_options(game, actor, state.extra.get("vote_candidates")),
        )
    if ability == "badge_transfer":
        return _target_panel(
            prompt, actor, ability, "🎖️ **警徽**：你出局了。選擇要把警徽交給誰，或撕毀警徽（之後沒有警長）。", "選擇接收警徽的玩家",
            lambda t: f"你把警徽交給了 {N.label(game, t)}。", skip_label="撕徽", skip_text="你撕毀了警徽。",
            options=[p for p in N.alive(game) if p.id != actor.id],
        )
    if ability == "day_skill_target":
        from . import day as D
        return _target_panel(
            prompt, actor, ability, "🎯 選擇技能目標：", "選擇目標",
            lambda t: f"你選擇了 {N.label(game, t)}。", skip_label="取消",
            options=D.skill_targets(game, actor),
        )
    if ability == "guard_protect":
        return _target_panel(prompt, actor, ability, "🛡️ **守衛**：選擇今晚要守護的玩家。", "選擇守護對象",
                             lambda t: f"你守護了 {N.label(game, t)}。")
    if ability == "check_faction":
        return _target_panel(
            prompt, actor, ability, "🔮 **預言家**：選擇要查驗的玩家。", "選擇查驗對象",
            lambda t: f"🔮 {N.label(game, t)} 是 **{N.seer_result(real(t))}**。")
    if ability == "lucky_check":
        return _target_panel(
            prompt, actor, ability, "🍀 **幸運兒**：你獲得了一次查驗技能。", "選擇查驗對象",
            lambda t: f"🍀 {N.label(game, t)} 是 **{N.seer_result(real(t))}**。")
    if ability == "lucky_poison":
        return _target_panel(prompt, actor, ability, "🍀 **幸運兒**：你獲得了一次毒藥。", "選擇毒殺對象",
                             lambda t: f"你對 {N.label(game, t)} 使用了毒藥。",
                             options=[p for p in N.alive(game) if p.id != actor.id])
    if ability == "lucky_protect":
        return _target_panel(prompt, actor, ability, "🍀 **幸運兒**：你獲得了一次守護。", "選擇守護對象",
                             lambda t: f"你守護了 {N.label(game, t)}。", options=N.alive(game))
    if ability == "exact_role_check":
        icon = N.ROLE_EMOJI.get(actor.role_id, "🔍")
        return _target_panel(
            prompt, actor, ability, f"{icon} **{role['name']}**：選擇要查驗身分的玩家。", "選擇查驗對象",
            lambda t: f"{icon} {N.label(game, t)} 的身分是 **{game.role_of(real(t))['name']}**。")
    if ability == "curse_player":
        return _target_panel(
            prompt, actor, ability,
            f"🐦 **烏鴉**：選擇要詛咒的玩家，對方在明天的放逐投票會多 {N.settings(game)['crow_extra_votes']:g} 票。"
            "不能詛咒自己，也不能連續兩晚同一人。也可以不發動。",
            "選擇詛咒對象", lambda t: f"你詛咒了 {N.label(game, t)}。")
    if ability == "bind_three":
        if N.resource_left(actor, "bind_charge") <= 0:
            return info("🧿 你的占卜已經用過了。")
        return _target_panel(
            prompt, actor, ability,
            "🧿 **占卜師**：整局只能發動一次。指定一位玩家，他和左右兩邊的鄰居共三人被綁定，"
            "今晚狼人只能襲擊這三人其中一個。也可以不發動（不算用掉）。",
            "選擇綁定的玩家", lambda t: f"你綁定了 {N.label(game, t)} 與他左右的鄰居。",
            skip_label="不發動", skip_text="你選擇不發動。")
    if ability == "silence_player":
        return _target_panel(
            prompt, actor, ability,
            "🤐 **禁言長老**：選擇要禁言的玩家，對方明天白天不能發言（仍可投票）。不能禁言自己，也不能連續兩晚同一人。也可以不發動。",
            "選擇禁言對象", lambda t: f"你禁言了 {N.label(game, t)}。")
    if ability == "fear_player":
        return _target_panel(
            prompt, actor, ability,
            "😱 **夢魘**：選擇要恐懼的玩家，對方今晚無法使用技能。不能恐懼自己，也不能連續兩晚恐懼同一人，第一夜不能恐懼狼隊友。"
            "恐懼狼人會讓狼隊今晚無法襲擊。", "選擇恐懼對象",
            lambda t: f"你恐懼了 {N.label(game, t)}。", mandatory=True)
    if ability == "assign_sleepwalker":
        return _target_panel(prompt, actor, ability, "💤 **攝夢人**：必須選擇一名夢遊者（連續兩晚選同一人，該人會死亡）。",
                             "選擇夢遊者", lambda t: f"你選擇了 {N.label(game, t)} 作為夢遊者。", mandatory=True)
    if ability == "charm_player":
        return _target_panel(prompt, actor, ability, "💋 **狼美人**：選擇要魅惑的玩家（不可連續兩晚同一人）。",
                             "選擇魅惑對象", lambda t: f"你魅惑了 {N.label(game, t)}。")
    if ability == "hunt_suspect":
        return _target_panel(
            prompt, actor, ability, "⚔️ **獵魔人**：選擇要狩獵的玩家。對方是狼人則出局，是好人則你出局。", "選擇狩獵對象",
            lambda t: f"你狩獵了 {N.label(game, t)}。")
    if ability == "choose_role_model":
        return _target_panel(prompt, actor, ability, "🌱 **野孩子**：選擇你的榜樣。榜樣出局後你會變成狼人。",
                             "選擇榜樣", lambda t: f"你選擇了 {N.label(game, t)} 作為榜樣。", mandatory=True)
    if ability == "mimic_target_ability":
        return _target_panel(prompt, actor, ability, "🤖 **機械狼**：選擇要學習技能的玩家（第二夜起可使用）。",
                             "選擇學習對象", lambda t: f"你學習了 {N.label(game, t)} 的技能。")
    if ability in ("bind_lovers", "swap_two_players") and len(N.options_for(game, state, actor, ability)) < 2:
        return info("場上可選的玩家不足兩人，無法發動技能。")
    if ability == "pick_bottom_card_role" and not game.bottom_cards:
        return info("沒有底牌可以選擇。")
    if ability == "grant_lucky_skill" and not N.options_for(game, state, actor, ability):
        return info("沒有其他玩家可以選擇。")
    if ability == "bind_lovers":
        header = "💘 **邱比特**：選擇兩名玩家成為情侶（可包含自己）。"
        return header, PairPanel(prompt, actor, "lovers", N.options_for(game, state, actor, ability), header,
                                 lambda p: f"你連結了 {N.label(game, p[0])} 與 {N.label(game, p[1])}。",
                                 skip_label=None)
    if ability == "swap_two_players":
        header = "🎩 **魔術師**：選擇兩名玩家交換號碼牌（每人整局只能被交換一次）。"
        return header, PairPanel(prompt, actor, "swap", N.options_for(game, state, actor, ability), header,
                                 lambda p: f"你交換了 {N.label(game, p[0])} 與 {N.label(game, p[1])}。")
    if ability == "pick_bottom_card_role":
        panel = ThiefPanel(prompt, actor)
        return panel.header(), panel
    if ability == "grant_lucky_skill":
        if N.resource_left(actor, "miracle_gift") <= 0:
            return info("🎁 你的禮物已經用完了。")
        panel = GiftPanel(prompt, actor)
        return panel._status(), panel
    if ability == "use_antidote":
        panel = WitchPanel(prompt, actor)
        return panel.header(), panel
    if ability == "check_previous_exiled_faction":
        gone = game.last_exiled
        if gone is None:
            return info("⚰️ **守墓人**：昨天沒有人被放逐。")
        side = "狼人" if game.players[gone].state["faction"] == "WEREWOLF" else "好人"
        return info(f"⚰️ **守墓人**：昨天被放逐的 {N.label(game, gone)} 是 **{side}**。")
    if ability == "lovers_info":
        other = game.lovers[0] if game.lovers[1] == actor.id else game.lovers[1]
        text = f"💞 你的情侶是 **{N.label(game, other)}**。"
        if game.lovers_mixed:
            text += "\n你們是第三方陣營：消滅其他所有人，並存活到最後。"
        return info(text)
    raise ValueError(f"no panel for {ability}")


class TurnView(discord.ui.View):
    """Public 'act' button for a turn. Only the players whose turn it is get a panel."""

    def __init__(self, prompt: Prompt):
        super().__init__(timeout=prompt.turn.seconds + 5)
        self.prompt = prompt

    @discord.ui.button(label="行動", style=discord.ButtonStyle.primary, emoji="👁️")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.prompt
        actor = p.actor_for(interaction.user.id)
        if p.closed:
            await interaction.response.send_message("這個回合已結束。", ephemeral=True)
        elif actor is None:
            await interaction.response.send_message("現在不是你的回合。", ephemeral=True)
        elif actor.id not in p.pending:
            await interaction.response.send_message("你已經行動過了。", ephemeral=True)
        else:
            text, view = make_panel(p, actor)
            await interaction.response.send_message(text, view=view, ephemeral=True)


# ---------------------------------------------------------------- test-mode bot players
async def auto_act(prompt: Prompt, actor: Player) -> None:
    """A bot player acts at random so a single tester can run a full game."""
    await asyncio.sleep(random.uniform(*config.FAKE_ACT_DELAY))
    game, state, ability = prompt.game, prompt.state, prompt.turn.ability_id
    role = game.role_of(actor)
    if state.sealed and role["initial_state"]["category"] == "GOD" and ability not in ("hunter_shot", "day_vote"):
        return
    pick = lambda options, chance=1.0: (  # noqa: E731
        random.choice(options).id if options and random.random() < chance else None
    )
    if ability == "wolf_pack_attack":
        state.record(ability, actor.id, pick(N.wolf_options(game, state), 0.9))
        if prompt.on_record:
            await prompt.on_record()
    elif ability == "hunter_shot":
        state.record(ability, actor.id, pick([p for p in N.alive(game) if p.id != actor.id], 0.7))
    elif ability == "sheriff_vote":
        from . import day as D
        state.record(ability, actor.id, pick(D.vote_options(game, actor, state.extra.get("vote_candidates")), 0.9))
    elif ability == "badge_transfer":
        state.record(ability, actor.id, pick([p for p in N.alive(game) if p.id != actor.id], 0.7))
    elif ability == "day_vote":
        from . import day as D
        state.record(ability, actor.id, pick(D.vote_options(game, actor, state.extra.get("vote_candidates")), 0.85))
    elif ability in ("curse_player", "silence_player"):
        state.record(ability, actor.id, pick(N.options_for(game, state, actor, ability), 0.75))
    elif ability in ("guard_protect", "check_faction", "lucky_check", "lucky_poison", "lucky_protect",
                     "exact_role_check", "assign_sleepwalker", "charm_player", "choose_role_model",
                     "mimic_target_ability", "fear_player"):
        state.record(ability, actor.id, pick(N.options_for(game, state, actor, ability)))
    elif ability == "bind_three":
        state.record(ability, actor.id, pick(N.options_for(game, state, actor, ability), 0.4)
                     if N.resource_left(actor, "bind_charge") > 0 else None)
    elif ability == "hunt_suspect":
        state.record(ability, actor.id, pick(N.options_for(game, state, actor, ability), 0.5))
    elif ability == "bind_lovers":
        if len(N.alive(game)) >= 2:
            state.extra.setdefault("lovers", {})[actor.id] = [p.id for p in random.sample(N.alive(game), 2)]
    elif ability == "swap_two_players":
        options = N.options_for(game, state, actor, ability)
        if len(options) >= 2 and random.random() < 0.6:
            state.extra.setdefault("swap", {})[actor.id] = [p.id for p in random.sample(options, 2)]
    elif ability == "pick_bottom_card_role":
        state.extra.setdefault("thief_pick", {})[actor.id] = random.choice(N.thief_options(game))
    elif ability == "grant_lucky_skill":
        options = N.options_for(game, state, actor, ability)
        if N.resource_left(actor, "miracle_gift") > 0 and options and random.random() < 0.7:
            state.record(ability, actor.id, random.choice(options).id)
            state.extra.setdefault("gift", {})[actor.id] = random.choice(list(N.GIFTS))
    elif ability == "use_antidote":
        o = N.witch_options(game, actor, state)
        if o["can_heal"] and random.random() < 0.5:
            N.use_resource(actor, "antidote")
            state.record("use_antidote", actor.id, o["victim"])
        elif o["can_poison"] and random.random() < 0.3:
            N.use_resource(actor, "poison")
            options = N.options_for(game, state, actor, "use_poison")
            if options:
                state.record("use_poison", actor.id, random.choice(options).id)


async def wait_for_actors(prompt: Prompt, fakes: list, seconds: float, public: bool = False) -> None:
    """Wait until every real actor and every bot has acted, or the time is up. 30 and 10 seconds before the end
    the ones who have not acted yet are reminded: in the public channel for a public turn (votes, shots), in the
    wolf channel for the wolves, otherwise in each one's own private channel (nothing public at night)."""
    game = prompt.game

    async def remind(left: int) -> None:
        waiting = [a for a in prompt.actors if a.id in prompt.pending]
        if not waiting or prompt.closed:
            return
        text = f"⏰ 剩 **{left} 秒**！還沒行動的請盡快按「行動」。"
        if public:
            await game.channel.send(f"{ping(waiting)} {text}")
            await beep(game, game.voice_channel, left)  # the day: everybody is in the main voice channel
        elif prompt.turn.key == "wolves" and game.wolf_voice:
            await game.wolf_voice.send(f"{ping(waiting)} {text}")
            await beep(game, game.wolf_voice, left)
        else:
            for actor in waiting:
                channel = game.private_channels.get(actor.id) or game.channel
                await channel.send(f"{actor.member.mention} {text}")
            await beep(game, game.private_channels.get(waiting[0].id), left)  # at night: the channel of whoever is acting

    async def everyone():
        await asyncio.gather(prompt.event.wait(), *fakes)

    warn = start_warnings(seconds, remind)
    try:
        await asyncio.wait_for(everyone(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    finally:
        stop_warnings(warn)


# ---------------------------------------------------------------- the night
async def run_turn(game: Game, turn: N.Turn, state: N.NightState) -> None:
    """One night turn. The 'act' button is posted in each actor's own private channel (the wolves
    share the wolf channel), so nothing about whose turn it is appears in the public channel."""
    prompt = Prompt(game, turn, state)
    wolves = turn.key == "wolves"
    emoji = "🐺" if wolves else turn.emoji
    posted: list[tuple[discord.Message, TurnView]] = []
    board = None

    wolf_chat = game.wolf_voice if wolves else None
    if prompt.pending and wolf_chat:
        view = TurnView(prompt)
        posted.append((await wolf_chat.send(
            f"{ping(prompt.actors)} 🔔 **輪到狼人了！**\n"
            f"你們已被移到這個語音頻道，請討論後在 {countdown(turn.seconds)} 前按「行動」投票襲擊目標。"
            "選「棄權」代表不襲擊。", view=view), view))
        board = await wolf_chat.send(wolf_board_text(prompt))  # live board of each wolf's vote

        async def refresh():
            await board.edit(content=wolf_board_text(prompt))
        prompt.on_record = refresh
    elif prompt.pending:
        for actor in prompt.actors:
            if actor.is_fake:
                continue
            channel = game.private_channels.get(actor.id) or game.channel  # public only if there is no room
            view = TurnView(prompt)
            posted.append((await channel.send(
                f"{actor.member.mention} 🔔 **輪到你了！**\n"
                f"{emoji} **{turn.name}**請睜眼，請在 {countdown(turn.seconds)} 前按下方「行動」。", view=view), view))

    try:
        if not prompt.actors:  # role is dead: wait a while so nobody can tell
            await asyncio.sleep(random.uniform(*config.DEAD_ROLE_DELAY))
        else:
            if wolves:
                await move_wolves(game, True, prompt.actors)
            fakes = [asyncio.create_task(auto_act(prompt, a)) for a in prompt.actors if a.is_fake]
            await wait_for_actors(prompt, fakes, turn.seconds)
    finally:
        prompt.closed = True
        if wolves:
            await move_wolves(game, False)
    N.finish_turn(game, state, turn)  # permanent effects, before the next turn is chosen
    if board:
        target = state.wolf_target
        result = f"最終決定：襲擊 **{N.label(game, target)}**" if target is not None else "最終決定：**今晚不襲擊**"
        await board.edit(content=wolf_board_text(prompt, final=True) + "\n" + result)
    for message, view in posted:
        view.stop()
        await message.edit(content=f"{emoji} **{turn.name}**請閉眼。", view=None)


async def collect(game: Game, turn: N.Turn, state: N.NightState, opening: str, closing: str,
                  allow_dead: bool = False) -> None:
    """Public prompt with an 'act' button, then wait until every real actor has acted or time is up.
    The players whose turn it is get pinged. Bot players act on their own."""
    prompt = Prompt(game, turn, state, allow_dead)
    view = TurnView(prompt) if prompt.pending else None
    if prompt.pending:
        text = f"{ping(prompt.actors)} 🔔 {opening}\n請在 {countdown(turn.seconds)} 前按下方「行動」。"
        msg = await game.channel.send(text, view=view)  # not say(): the mentions must notify
    else:
        msg = await say(game, opening)
    fakes = [asyncio.create_task(auto_act(prompt, a)) for a in prompt.actors if a.is_fake]
    try:
        await wait_for_actors(prompt, fakes, turn.seconds, public=True)
    finally:
        prompt.closed = True
        for task in fakes:
            task.cancel()
        if view:
            view.stop()
        await msg.edit(content=closing, view=None)  # also when the round is cut short (a self-destruct)


async def tell_feared(game: Game, state: N.NightState) -> None:
    """Only if the rule 'nightmare_fear_notice' is on: tell the feared (and their pack) privately.
    By default nobody is told, they just do not get their turn."""
    if not state.feared or not N.settings(game)["nightmare_fear_notice"]:
        return
    notes = {pid: "😱 你今晚被夢魘恐懼，技能無法使用。" for pid in state.feared}
    if N.wolves_blocked(game, state):
        for wolf in N.wolves_turn_actors(game):
            if wolf.state["is_alive"] and wolf.id not in notes:
                notes[wolf.id] = "😱 狼隊有人被夢魘恐懼，今晚無法襲擊。"
    for pid, text in notes.items():
        channel = game.private_channels.get(pid)
        if channel:
            await channel.send(f"{game.players[pid].member.mention} {text}")


async def run_night(game: Game) -> N.NightResult:
    """Play one night and return what happened. The day announces it."""
    game.night_number += 1
    state = N.NightState(game.night_number)
    state.sealed, game.sealed_tonight = game.sealed_tonight, False  # blood moon apostle's seal
    game.night = state
    living = sorted(N.alive(game), key=lambda p: p.seat)
    roster = "、".join(f"{p.seat}號 {p.member.display_name}{'🎖️' if p.id == game.sheriff else ''}" for p in living)
    await say(game, (f"🌙 **第 {state.number} 夜**，天黑請閉眼。\n"
                     f"目前存活（{len(living)} 人）：{roster}")[:1900])
    game.voice_phase = "night"
    await isolate_players(game)  # everyone to their own channel (unmuted), so the wolves leaving shows nothing

    while (turn := N.next_turn(game, state)) is not None:
        await run_turn(game, turn, state)
        await refresh_cards(game)  # a new role, a lover, a role model... goes to the player's channel
        if turn.ability_id == "fear_player":
            await tell_feared(game, state)
    game.feared_last_night = set(state.feared)

    result = N.resolve_night(game, state)
    game.history += [f"第{state.number}夜：{line}" for line in result.log]
    return result
