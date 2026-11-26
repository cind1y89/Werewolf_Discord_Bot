"""The sheriff as a Discord flow: the election after the first night, the speaking direction,
and the badge when the sheriff dies."""
from __future__ import annotations

import asyncio
import random

import discord

from . import config
from . import day as D
from . import night as N
from . import sheriff as S
from .models import Game, Player
from .night_flow import collect, countdown, lock_voice, ping, say
from .win import evaluate


def seat_label(game: Game, pid: int) -> str:
    return N.label(game, pid)


async def settle_sheriff(game: Game, pid: int, how: str) -> None:
    game.sheriff = pid
    game.history.append(f"警長：{seat_label(game, pid)}（{how}）")
    await say(game, f"🎖️ **{seat_label(game, pid)}** 當選警長！{how}。放逐投票算 1.5 票，並決定白天的發言順序。")


# ---------------------------------------------------------------- sign-up: 上警 / 不上警
class SignupView(discord.ui.View):
    """Everyone alive picks 上警 or 不上警 (they can change their mind until the time is up)."""

    def __init__(self, game: Game):
        super().__init__(timeout=config.SHERIFF_SIGNUP_SECONDS + 5)
        self.game = game
        self.choices: dict[int, bool] = {}
        self.event = asyncio.Event()
        self.waiting = {p.id for p in N.alive(game) if not p.is_fake}
        if not self.waiting:
            self.event.set()

    async def _choose(self, interaction: discord.Interaction, run: bool) -> None:
        player = self.game.players.get(interaction.user.id)
        if player is None or not player.state.get("is_alive"):
            await interaction.response.send_message("只有存活的玩家可以選擇。", ephemeral=True)
            return
        self.choices[player.id] = run
        self.waiting.discard(player.id)
        if not self.waiting:
            self.event.set()
        await interaction.response.send_message("你選擇了「上警」🙋" if run else "你選擇了「不上警」🙅", ephemeral=True)

    @discord.ui.button(label="上警", style=discord.ButtonStyle.success, emoji="🙋")
    async def run(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, True)

    @discord.ui.button(label="不上警", style=discord.ButtonStyle.secondary, emoji="🙅")
    async def stay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, False)


async def signup(game: Game) -> list[Player]:
    """Returns the candidates (上警), in seat order."""
    view = SignupView(game)
    living = N.alive(game)
    msg = await game.channel.send(
        f"{ping(living)} 🎖️ **警長競選：請選擇「上警」或「不上警」**\n"
        f"請在 {countdown(config.SHERIFF_SIGNUP_SECONDS)} 前選擇。沒選的視為不上警。", view=view)
    for p in living:  # bots decide for themselves
        if p.is_fake:
            view.choices[p.id] = random.random() < 0.35
    try:
        try:
            await asyncio.wait_for(view.event.wait(), timeout=config.SHERIFF_SIGNUP_SECONDS)
        except asyncio.TimeoutError:
            pass
    finally:
        view.stop()
        await msg.edit(content="🎖️ 警長競選報名結束。", view=None)
    return [p for p in sorted(living, key=lambda p: p.seat) if view.choices.get(p.id)]


# ---------------------------------------------------------------- campaign speeches and 退水
class CampaignView(discord.ui.View):
    """A candidate's speech: 跳過 ends it, 退水 ends it and drops out of the race."""

    def __init__(self, game: Game, speaker: Player, withdrawn: set[int], can_withdraw: bool = True):
        super().__init__(timeout=game.speak_seconds + 5 if game.speak_seconds else None)
        self.game, self.speaker, self.withdrawn = game, speaker, withdrawn
        self.event = asyncio.Event()
        if not can_withdraw:
            self.remove_item(self.withdraw)

    @discord.ui.button(label="跳過", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.speaker.id, self.game.host.id):
            await interaction.response.send_message("只有發言者或房主可以跳過。", ephemeral=True)
            return
        self.event.set()
        await interaction.response.defer()

    @discord.ui.button(label="退水", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.speaker.id:
            await interaction.response.send_message("只有發言的候選人可以退水。", ephemeral=True)
            return
        self.withdrawn.add(self.speaker.id)
        self.event.set()
        await interaction.response.send_message("你已退水，不再競選警長。", ephemeral=True)


async def campaign(game: Game, speakers: list[Player], withdrawn: set[int], can_withdraw: bool = True,
                   title: str = "🎤 警上發言") -> None:
    """Each candidate speaks in turn with the usual speaking time."""
    for speaker in speakers:
        if speaker.id in withdrawn or not speaker.state["is_alive"]:
            continue
        if speaker.id in game.silenced:
            await say(game, f"🤐 **{seat_label(game, speaker.id)}** 被禁言，今天不能發言（仍可參選與投票）。")
            continue
        await lock_voice(game, {speaker.id})
        view = CampaignView(game, speaker, withdrawn, can_withdraw)
        timing = f"請在 {countdown(game.speak_seconds)} 前說完" if game.speak_seconds else "不限時，說完請按「跳過」"
        msg = await game.channel.send(
            f"{speaker.member.mention} {title}（{seat_label(game, speaker.id)}）\n{timing}。"
            + ("若不想競選了可以按「退水」。" if can_withdraw else ""), view=view)
        try:
            if speaker.is_fake:
                await asyncio.sleep(random.uniform(*config.FAKE_ACT_DELAY))
                if can_withdraw and random.random() < 0.12:
                    withdrawn.add(speaker.id)
            else:
                try:
                    await asyncio.wait_for(view.event.wait(), timeout=game.speak_seconds or None)
                except asyncio.TimeoutError:
                    pass
        finally:
            view.stop()
            await lock_voice(game, set())
            done = "退水了" if speaker.id in withdrawn else "發言結束"
            await msg.edit(content=f"🎤 {seat_label(game, speaker.id)} {done}。", view=None)


class WithdrawWindow(discord.ui.View):
    """After the speeches: each candidate confirms they stay in, or 退水."""

    def __init__(self, candidates: list[Player], withdrawn: set[int]):
        super().__init__(timeout=config.SHERIFF_WITHDRAW_SECONDS + 5)
        self.candidates, self.withdrawn = {p.id for p in candidates}, withdrawn
        self.waiting = {p.id for p in candidates if not p.is_fake}
        self.event = asyncio.Event()
        if not self.waiting:
            self.event.set()

    async def _answer(self, interaction: discord.Interaction, leave: bool) -> None:
        if interaction.user.id not in self.candidates:
            await interaction.response.send_message("只有上警的玩家可以選擇。", ephemeral=True)
            return
        (self.withdrawn.add if leave else self.withdrawn.discard)(interaction.user.id)
        self.waiting.discard(interaction.user.id)
        if not self.waiting:
            self.event.set()
        await interaction.response.send_message("你已退水。" if leave else "你確定繼續參選。", ephemeral=True)

    @discord.ui.button(label="退水", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, True)

    @discord.ui.button(label="繼續參選", style=discord.ButtonStyle.success, emoji="✅")
    async def stay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, False)


async def withdraw_window(game: Game, candidates: list[Player], withdrawn: set[int]) -> None:
    remaining = [c for c in candidates if c.id not in withdrawn]
    view = WithdrawWindow(remaining, withdrawn)
    msg = await game.channel.send(
        f"{ping(remaining)} 🏳️ **退水時間**：不想競選的人現在可以退水，退水後不能再當警長，也不能投票。\n"
        f"請在 {countdown(config.SHERIFF_WITHDRAW_SECONDS)} 前選擇（不選視為繼續參選）。", view=view)
    for c in remaining:
        if c.is_fake and random.random() < 0.15:
            withdrawn.add(c.id)
    try:
        try:
            await asyncio.wait_for(view.event.wait(), timeout=config.SHERIFF_WITHDRAW_SECONDS)
        except asyncio.TimeoutError:
            pass
    finally:
        view.stop()
        await msg.edit(content="🏳️ 退水結束。", view=None)


# ---------------------------------------------------------------- the vote
async def ballot(game: Game, candidates: list[Player], exclude: set[int], opening: str):
    """Everyone alive who is not in `exclude` votes among the candidates. Returns (winner, tied)."""
    state = N.NightState(game.night_number)
    ids = [c.id for c in candidates]
    state.extra["vote_candidates"] = set(ids)
    voters = [p for p in N.alive(game) if p.id not in exclude]
    turn = N.Turn("sheriff_vote", "警長投票", "🎖️", "sheriff_vote", 0, config.DAY_VOTE_SECONDS, voters)
    if not voters:
        return None, []
    await collect(game, turn, state, opening, "🎖️ 警長投票結束。")
    votes = state.acts.get("sheriff_vote", {})
    winner, tied, counts = S.tally_sheriff(votes, ids)
    seat = lambda i: game.players[i].seat  # noqa: E731
    by_target: dict[int, list[int]] = {t: [] for t in ids}
    abstain: list[int] = []
    for voter, target in votes.items():
        if target in by_target:
            by_target[target].append(voter)
        else:
            abstain.append(voter)
    who = lambda vs: "、".join(f"{seat(v)}號" for v in sorted(vs, key=seat)) or "無人投票"  # noqa: E731
    names = lambda vs: [f"　{seat_label(game, v)}" for v in sorted(vs, key=seat)] or ["　無人投票"]  # noqa: E731
    silent = [p.id for p in voters if p.id not in votes]
    shown: list[str] = []  # one voter per line below each candidate, no brackets
    record: list[str] = []  # the one-line form for the game record
    for t in sorted(ids, key=lambda t: (-len(by_target[t]), seat(t))):
        shown += [f"• {seat_label(game, t)}：{len(by_target[t])} 票"] + names(by_target[t])
        record.append(f"{seat_label(game, t)}：{len(by_target[t])} 票（{who(by_target[t])}）")
    if abstain or silent:
        shown += [f"• 棄權：{len(abstain) + len(silent)} 票"] + names(abstain + silent)
        record.append(f"棄權：{len(abstain) + len(silent)} 票（{who(abstain + silent)}）")
    await say(game, ("📊 **警長投票結果**\n" + "\n".join(shown))[:1900])
    game.history.append("警長投票：" + "；".join(record))
    return winner, tied


async def _election_body(game: Game) -> None:
    """The election itself: sign-up, speeches, 退水, vote. Leaves game.sheriff set, or nobody (流局)."""
    living = N.alive(game)
    if len(living) < 2:
        return
    await say(game, "🎖️ **警長競選開始！** 想當警長的人上警，其餘的人是警下，負責投票。")
    candidates = await signup(game)
    if not candidates:
        await say(game, "沒有人上警，本局沒有警長。")
        game.history.append("警長競選：沒有人上警，流局")
        return
    await say(game, "🙋 **上警：** " + "、".join(seat_label(game, c.id) for c in candidates)
              + "\n🙅 **警下：** " + ("、".join(seat_label(game, p.id) for p in living if p not in candidates) or "（無）"))
    if len(candidates) == 1:
        await settle_sheriff(game, candidates[0].id, "只有一位上警，直接當選")
        return

    withdrawn: set[int] = set()
    order = D.speaking_order(game, only={c.id for c in candidates})
    await say(game, "🎤 警上發言順序：" + " → ".join(f"{p.seat}號" for p in order))
    await campaign(game, order, withdrawn)
    if len(candidates) - len(withdrawn) > 1:
        await withdraw_window(game, candidates, withdrawn)
    left = [c for c in candidates if c.id not in withdrawn]
    if withdrawn:
        await say(game, "🏳️ 退水：" + "、".join(seat_label(game, i) for i in sorted(withdrawn, key=lambda i: game.players[i].seat)))
    if not left:
        await say(game, "所有上警的人都退水了，本局沒有警長（流局）。")
        game.history.append("警長競選：全部退水，流局")
        return
    if len(left) == 1:
        await settle_sheriff(game, left[0].id, "其他人都退水，直接當選")
        return

    runners = {c.id for c in candidates}  # everyone who ran, 退水 or not, is 警上: no vote
    if not [p for p in living if p.id not in runners]:
        await say(game, "沒有警下玩家可以投票，本局沒有警長（流局）。")
        return
    winner, tied = await ballot(game, left, runners,
                                "🎖️ **警長投票**：只有警下玩家投票（上警的人，包含退水的，都不能投票），請選擇你支持的警上玩家（可棄權）。")
    if winner is None and tied:
        names = "、".join(seat_label(game, i) for i in tied)
        await say(game, f"⚖️ **平票！** {names} 同票：他們再各發言一輪，之後由其他玩家重新投票。")
        game.history.append(f"警長平票：{names}")
        speakers = [game.players[i] for i in sorted(tied, key=lambda i: game.players[i].seat)]
        await campaign(game, speakers, set(), can_withdraw=False, title="🎤 警長平票補充發言")
        winner, tied = await ballot(game, [game.players[i] for i in tied], runners,
                                    "🎖️ **警長平票投票**：請在平票的玩家之中選擇（可棄權）。")
    if winner is None:
        await say(game, "沒有選出警長（流局）。")
        game.history.append("警長競選：流局")
        return
    await settle_sheriff(game, winner, "得票最高")


# ---------------------------------------------------------------- speaking direction
class DirectionView(discord.ui.View):
    def __init__(self, sheriff: Player):
        super().__init__(timeout=config.SHERIFF_DIRECTION_SECONDS + 5)
        self.sheriff = sheriff
        self.choice: str | None = None
        self.event = asyncio.Event()

    async def _pick(self, interaction: discord.Interaction, direction: str) -> None:
        if interaction.user.id != self.sheriff.id:
            await interaction.response.send_message("只有警長可以決定。", ephemeral=True)
            return
        self.choice = direction
        self.event.set()
        await interaction.response.send_message("已決定發言順序。", ephemeral=True)

    @discord.ui.button(label="號碼遞增（順時針）", style=discord.ButtonStyle.primary, emoji="⬆️")
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "up")

    @discord.ui.button(label="號碼遞減（逆時針）", style=discord.ButtonStyle.primary, emoji="⬇️")
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "down")


async def speaking_order_today(game: Game) -> list[Player] | None:
    """The sheriff picks the direction and speaks last. None (random order) when there is no living sheriff."""
    if not S.is_sheriff_alive(game):
        return None
    sheriff = game.players[game.sheriff]
    if sheriff.is_fake:
        direction = S.random_direction()
    else:
        view = DirectionView(sheriff)
        msg = await game.channel.send(
            f"{sheriff.member.mention} 🎖️ **警長，請決定今天的發言順序**（你最後發言）\n"
            f"請在 {countdown(config.SHERIFF_DIRECTION_SECONDS)} 前選擇，不選擇隨機。", view=view)
        try:
            await asyncio.wait_for(view.event.wait(), timeout=config.SHERIFF_DIRECTION_SECONDS)
        except asyncio.TimeoutError:
            pass
        view.stop()
        await msg.edit(content="🎖️ 警長已決定發言順序。", view=None)
        direction = view.choice or S.random_direction()
    order = S.sheriff_order(game, direction)
    arrow = "號碼遞增（順時針）" if direction == "up" else "號碼遞減（逆時針）"
    await say(game, f"🎖️ 警長決定發言順序：{arrow}，警長最後發言。")
    return order


# ---------------------------------------------------------------- the badge
async def settle_badge(game: Game) -> None:
    """If the sheriff has died: hand the badge to a living player, or tear it up (no more sheriff).
    Does nothing when the game is already decided."""
    pid = game.sheriff
    if pid is None or game.players[pid].state.get("is_alive") or evaluate(game):
        return
    sheriff = game.players[pid]
    game.sheriff = None
    if len(N.alive(game)) < 1:
        return
    state = N.NightState(game.night_number)
    turn = N.Turn("badge", "警徽", "🎖️", "badge_transfer", 0, config.SHERIFF_BADGE_SECONDS, [sheriff])
    await collect(game, turn, state,
                  f"🎖️ 警長 **{seat_label(game, pid)}** 出局了，請決定把警徽移交給誰，或撕毀警徽。",
                  "🎖️ 警徽處理完畢。", allow_dead=True)
    target = state.acts.get("badge_transfer", {}).get(pid)
    if target is not None and game.players[target].state["is_alive"]:
        game.sheriff = target
        game.history.append(f"警徽：{seat_label(game, pid)} 移交給 {seat_label(game, target)}")
        await say(game, f"🎖️ 警徽移交給 **{seat_label(game, target)}**，他現在是警長。")
    else:
        game.badge_torn = True
        game.history.append(f"警徽：{seat_label(game, pid)} 撕毀了警徽")
        await say(game, "🎖️ 警徽被撕毀了，之後沒有警長。")


# ---------------------------------------------------------------- self-destruct during the election
EXPLODE_SKILLS = {"wolf_suicide", "white_wolf_explode_kill", "blood_moon_explode_seal"}


def booms_needed(game: Game) -> int:
    """How many self-destructing wolves swallow the badge: 單爆 (1) for small games, 雙爆 (2) from 11 players.
    The rule 'sheriff_swallow_booms' overrides it (0 = decide by player count)."""
    return game.swallow_booms()


def election_explosions(game: Game, player: Player) -> list[str]:
    if player.id in game.election_hidden:  # already dead in the night: cannot blow up
        return []
    return [s for s in D.day_skills(game, player) if s in EXPLODE_SKILLS]


class BoomView(discord.ui.View):
    """One 自爆 button that stays up for the whole election (sign-up, speeches, 退水, vote)."""

    def __init__(self, game: Game, speech):
        super().__init__(timeout=None)
        self.game, self.speech = game, speech

    @discord.ui.button(label="自爆", style=discord.ButtonStyle.danger, emoji="💥")
    async def boom(self, interaction: discord.Interaction, button: discord.ui.Button):
        from .day_flow import SkillPanel
        player = self.game.players.get(interaction.user.id)
        skills = election_explosions(self.game, player) if player else []
        if not skills:
            await interaction.response.send_message("你不能自爆（只有還能自爆的狼人可以按）。", ephemeral=True)
            return
        await interaction.response.send_message(
            "選擇要發動的技能：", view=SkillPanel(self.game, player, skills, self.speech), ephemeral=True)


async def _bot_boom(game: Game, speech) -> None:
    """Test mode: now and then a bot wolf blows up during the election."""
    await asyncio.sleep(random.uniform(3, 20))
    if random.random() > 0.15:
        return
    bots = [p for p in N.alive(game) if p.is_fake and election_explosions(game, p)]
    if bots:
        bot = random.choice(bots)
        skill = random.choice(election_explosions(game, bot))
        target = random.choice(D.skill_targets(game, bot)).id if skill in D.SKILLS_NEEDING_TARGET else None
        speech.submit(bot, skill, target)


async def run_election(game: Game):
    """The 警長競選, during which a wolf may 自爆. Returns (the day ended suddenly, outcome if decided).

    A self-destruct stops the election at once and sends everyone to night. When enough wolves have
    blown up (booms_needed), the badge is swallowed: nobody is sheriff for the rest of the game.
    Before that, the election is held again the next day."""
    from .day_flow import SpeechState, handle_skill
    if len(N.alive(game)) < 2:
        return False, None
    speech = SpeechState()
    msg = await game.channel.send("💥 競選期間，狼人可以按這裡「自爆」。", view=BoomView(game, speech))
    body = asyncio.create_task(_election_body(game))
    helper = asyncio.create_task(_bot_boom(game, speech))
    try:
        while True:
            waiter = asyncio.create_task(speech.queue.get())
            try:
                await asyncio.wait({body, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not waiter.done():
                    waiter.cancel()
            if body.done():  # the election ended on its own
                body.result()  # raises if the election itself failed
                return False, None
            player, skill, target = waiter.result()
            if skill not in election_explosions(game, player):
                continue
            if skill in D.SKILLS_NEEDING_TARGET and (target is None or not game.players[target].state["is_alive"]):
                continue
            body.cancel()
            try:
                await body
            except asyncio.CancelledError:
                pass
            speech.open = False
            ended, outcome = await handle_skill(game, (player, skill, target))
            game.election_booms += 1
            need = booms_needed(game)
            if game.election_booms >= need:
                game.badge_torn = True
                game.sheriff = None
                await say(game, f"🎖️ 競選期間共 {game.election_booms} 隻狼人自爆，**警徽被吞了**，本局沒有警長。")
                game.history.append("警長競選：狼人自爆吞掉警徽")
            else:
                game.election_retry = True
                await say(game, f"🎖️ 競選期間有狼人自爆（{game.election_booms}/{need}），**今天不選警長**，"
                                "明天白天重新競選；再有狼人在競選時自爆，警徽就會被吞。")
                game.history.append("警長競選：狼人自爆，改到明天重選")
            return ended, outcome
    finally:
        speech.open = False
        for task in (body, helper):
            if not task.done():
                task.cancel()
        await msg.edit(content="💥 競選結束，不能再自爆。", view=None)
