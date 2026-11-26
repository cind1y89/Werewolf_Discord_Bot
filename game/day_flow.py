"""The day as a Discord flow: dawn, death shots, speeches with skills, the exile vote."""
from __future__ import annotations

import asyncio
import random

import discord

from . import config
from . import day as D
from . import night as N
from .deaths import apply_deaths
from .models import Game, Player
from .night_flow import OwnedView, PickSelect, collect, countdown, lock_voice, say
from .room import gather_players, move_wolves, refresh_cards
from .sheriff import weights as sheriff_weights
from .sheriff_flow import run_election, settle_badge, speaking_order_today
from .win import Outcome, evaluate


def seat_label(game: Game, pid: int) -> str:
    return N.label(game, pid)


# ---------------------------------------------------------------- death shots
async def choose_shot(game: Game, shooter: Player) -> int | None:
    turn = N.Turn("shot", "開槍", "🔫", "hunter_shot", 90, config.HUNTER_SHOT_SECONDS, [shooter])
    state = N.NightState(game.night_number)
    name = seat_label(game, shooter.id)
    role = game.role_of(shooter)["name"]
    await collect(game, turn, state, f"🔫 **{name}** 是{role}，發動技能！", f"🔫 {role}技能結束。", allow_dead=True)
    return state.acts.get("hunter_shot", {}).get(shooter.id)  # resolve_shots spends the bullet


async def shots_for(game: Game, deaths: list[tuple[int, list[str]]], tag: str,
                    blocked: frozenset | set = frozenset()) -> list[tuple[int, list[str]]]:
    """Run death shots for these deaths and announce who they took with them.
    `blocked` are players who may not shoot this time (the nightmare's target who died that night)."""
    for pid, causes in deaths:  # a shooter who cannot shoot is told why, privately
        player = game.players[pid]
        reason = "你當晚被夢魘恐懼，技能無法使用。" if pid in blocked and D.has_death_shot(game, player) \
            else D.why_no_shot(game, player, causes)
        channel = game.private_channels.get(pid)
        if reason and channel:
            role = game.role_of(player)["name"]
            await channel.send(f"{player.member.mention} 【{role}】的開槍技能無法發動。\n{reason}")
    shooters = [d for d in deaths if d[0] not in blocked]
    extra = await D.resolve_shots(game, shooters, lambda s: choose_shot(game, s))
    if extra:
        await say(game, "🔫 被帶走：" + "、".join(seat_label(game, pid) for pid, _ in extra))
        game.history += [f"{tag}{seat_label(game, pid)} 被帶走（{'、'.join(N.CAUSE_NAMES.get(c, c) for c in causes)}）。"
                         for pid, causes in extra]
    return extra


# ---------------------------------------------------------------- last words
async def last_words(game: Game, ids: list[int]) -> None:
    """遺言: each of these (dead) players speaks once, in seat order, with the usual speaking time.
    Only called for deaths that leave the day running, so a self-destruct never gets here."""
    for pid in sorted(dict.fromkeys(ids), key=lambda i: game.players[i].seat):
        player = game.players[pid]
        name = seat_label(game, pid)
        game.dead_may_speak.add(pid)
        await lock_voice(game, {pid})
        view = SpeechView(game, player, SpeechState(), skills=False)
        timing = f"請在 {countdown(game.speak_seconds)} 前說完" if game.speak_seconds else "不限時，說完請按「跳過」"
        msg = await game.channel.send(
            f"{player.member.mention} 🕯️ **遺言**（{name}）\n你出局了，這是你最後一次發言。{timing}。", view=view)
        try:
            if player.is_fake:
                await asyncio.sleep(random.uniform(*config.FAKE_ACT_DELAY))
            else:
                try:
                    await asyncio.wait_for(view.event.wait(), timeout=game.speak_seconds or None)
                except asyncio.TimeoutError:
                    pass
        finally:
            view.stop()
            game.dead_may_speak.discard(pid)
            await lock_voice(game, set())
        await msg.edit(content=f"🕯️ {name} 遺言結束。", view=None)
        game.history.append(f"{name} 留下遺言。")


async def announce_deaths(game: Game, deaths: list[tuple[int, list[str]]]) -> None:
    if deaths:
        await say(game, "☠️ 死亡：" + "、".join(seat_label(game, pid) for pid, _ in deaths))


# ---------------------------------------------------------------- dawn
async def dawn(game: Game, result: N.NightResult, greeted: bool = False) -> list[int]:
    """Announce the night. Returns who now gives last words: everyone who died in the FIRST night,
    anyone taken by a death shot, and a postponed exile; later nights give none.
    `greeted`: 天亮了 was already said (the sheriff election came first), so only the news follows."""
    dead = sorted((pid for pid, _ in result.deaths), key=lambda i: game.players[i].seat)
    if dead:
        await say(game, "☀️ **天亮了。** 昨晚死亡：" + "、".join(seat_label(game, pid) for pid in dead))
    else:
        await say(game, "☀️ **天亮了。** 昨晚是平安夜。")
    tag = f"第{game.night_number}夜："
    speakers = [pid for pid, _ in result.deaths] if game.night_number == 1 else []
    speakers += [pid for pid, _ in await shots_for(game, result.deaths, tag, blocked=game.feared_last_night)]

    delayed = D.process_delayed_deaths(game)  # blood moon apostle's postponed exile
    if delayed:
        names = "、".join(seat_label(game, pid) for pid, _ in delayed)
        await say(game, f"🩸 昨天被放逐的 **{names}** 撐過了一夜，現在死亡。")
        game.history.append(f"{tag}{names} 延遲死亡。")
        speakers += [pid for pid, _ in delayed]
        speakers += [pid for pid, _ in await shots_for(game, delayed, tag)]
    await settle_badge(game)  # a sheriff who died in the night hands over (or tears) the badge
    return speakers


# ---------------------------------------------------------------- skills during the speeches
class SkillTargetPanel(OwnedView):
    """Pick a target for a day skill, then 確認."""

    def __init__(self, game: Game, player: Player, skill: str, speech: "SpeechState"):
        super().__init__(player, 120)
        self.game, self.player, self.skill, self.speech = game, player, skill, speech
        self.selected: int | None = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(PickSelect(D.skill_targets(self.game, self.player), "選擇目標", self.selected))
        ok = discord.ui.Button(label="確認", style=discord.ButtonStyle.success, emoji="✅", row=1,
                               disabled=self.selected is None)
        ok.callback = self._confirm
        self.add_item(ok)
        cancel = discord.ui.Button(label="取消", style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def select(self, interaction: discord.Interaction, target_id: int):
        self.selected = target_id
        self._build()
        await interaction.response.edit_message(
            content=f"{D.SKILL_NAMES[self.skill]}：已選擇 **{seat_label(self.game, target_id)}**，請按「確認」。", view=self
        )

    async def _confirm(self, interaction: discord.Interaction):
        self.speech.submit(self.player, self.skill, self.selected)
        await interaction.response.edit_message(content="已發動技能。", view=None)

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="已取消。", view=None)


class SkillPanel(OwnedView):
    def __init__(self, game: Game, player: Player, skills: list[str], speech: "SpeechState"):
        super().__init__(player, 120)
        self.game, self.player, self.speech = game, player, speech
        for skill in skills:
            button = discord.ui.Button(label=D.SKILL_NAMES[skill], style=discord.ButtonStyle.danger)
            button.callback = self._choose(skill)
            self.add_item(button)

    def _choose(self, skill: str):
        async def callback(interaction: discord.Interaction):
            if skill in D.SKILLS_NEEDING_TARGET:
                panel = SkillTargetPanel(self.game, self.player, skill, self.speech)
                await interaction.response.edit_message(content=f"{D.SKILL_NAMES[skill]}：選擇目標。", view=panel)
                return

            async def confirmed(i: discord.Interaction):
                self.speech.submit(self.player, skill, None)
                await i.response.edit_message(content="已發動技能。", view=None)

            async def back(i: discord.Interaction):
                await i.response.edit_message(content="選擇要發動的技能：", view=self)

            from .night_flow import ConfirmStep
            await interaction.response.edit_message(
                content=f"確定要「{D.SKILL_NAMES[skill]}」嗎？", view=ConfirmStep(self.player, confirmed, back)
            )
        return callback


class SpeechState:
    """Skill requests made during the speeches, shared by every speaker's message."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.open = True

    def submit(self, player: Player, skill: str, target: int | None) -> None:
        if self.open:
            self.queue.put_nowait((player, skill, target))


class SpeechView(discord.ui.View):
    def __init__(self, game: Game, speaker: Player, speech: SpeechState, skills: bool = True):
        super().__init__(timeout=game.speak_seconds + 5 if game.speak_seconds else None)
        self.game, self.speaker, self.speech = game, speaker, speech
        self.event = asyncio.Event()
        if not skills:  # last words: no skill button
            self.remove_item(self.skill)

    @discord.ui.button(label="跳過", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.speaker.id, self.game.host.id):
            await interaction.response.send_message("只有發言者或房主可以跳過。", ephemeral=True)
            return
        self.event.set()
        await interaction.response.defer()

    @discord.ui.button(label="發動技能", style=discord.ButtonStyle.danger, emoji="💥")
    async def skill(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.game.players.get(interaction.user.id)
        skills = D.day_skills(self.game, player) if player else []
        if not skills:
            await interaction.response.send_message(D.no_day_skill_text(self.game, player), ephemeral=True)
            return
        await interaction.response.send_message(
            "選擇要發動的技能：", view=SkillPanel(self.game, player, skills, self.speech), ephemeral=True
        )


def bot_skill(game: Game, speech: SpeechState) -> None:
    """Test mode: now and then a bot player uses a day skill, so those paths get exercised."""
    if random.random() > 0.06:
        return
    bots = [p for p in N.alive(game) if p.is_fake and D.day_skills(game, p)]
    if not bots:
        return
    bot = random.choice(bots)
    skill = random.choice(D.day_skills(game, bot))
    target = random.choice(D.skill_targets(game, bot)).id if skill in D.SKILLS_NEEDING_TARGET else None
    speech.submit(bot, skill, target)


async def handle_skill(game: Game, request) -> tuple[bool, Outcome | None]:
    """Run one skill request. Returns (does the day end now, outcome if the game is decided)."""
    player, skill, target = request
    if skill not in D.day_skills(game, player):
        return False, None
    if skill in D.SKILLS_NEEDING_TARGET and (target is None or not game.players[target].state["is_alive"]):
        return False, None
    event = D.use_day_skill(game, player, skill, target)
    await say(game, event.text)
    game.history.append(f"第{game.night_number}天：" + event.text.replace("**", ""))
    await announce_deaths(game, event.deaths)  # everyone who died, links included
    extra = await shots_for(game, event.deaths, f"第{game.night_number}天：")
    await settle_badge(game)
    outcome = evaluate(game)
    if not event.ends_day and not outcome:
        # the day goes on (a failed knight duel): the dead get last words. A sudden night gives none.
        await last_words(game, [pid for pid, _ in event.deaths] + [pid for pid, _ in extra])
    return event.ends_day, outcome


async def run_speeches(game: Game, order: list[Player] | None = None,
                       title: str = "🎤 發言順序（順時針）：") -> tuple[bool, Outcome | None]:
    """Everyone speaks in turn, or just `order` (the 平票PK speakers).
    Returns (the day ended early by a skill, outcome if the game is decided)."""
    order = D.speaking_order(game) if order is None else order
    if not order:
        return False, None
    await say(game, title + " → ".join(f"{p.seat}號" + ("🎖️" if p.id == game.sheriff else "") for p in order))
    speech = SpeechState()
    loop = asyncio.get_running_loop()
    try:
        for speaker in order:
            if not speaker.state["is_alive"]:
                continue
            if speaker.id in game.silenced:  # 禁言: stays muted, their turn is skipped
                await say(game, f"🤐 **{seat_label(game, speaker.id)}** 被禁言，今天不能發言（仍可投票）。")
                continue
            await lock_voice(game, {speaker.id})
            view = SpeechView(game, speaker, speech)
            name = seat_label(game, speaker.id)
            timing = f"請在 {countdown(game.speak_seconds)} 前說完" if game.speak_seconds else "不限時，說完請按「跳過」"
            msg = await game.channel.send(  # not say(): the mention must notify the speaker
                f"{speaker.member.mention} 🎤 **輪到你發言了！**（{name}）\n{timing}。", view=view)
            if speaker.is_fake:
                deadline = loop.time() + random.uniform(*config.FAKE_ACT_DELAY)
            else:  # no deadline when speaking time is unlimited: only 跳過 ends the turn
                deadline = loop.time() + game.speak_seconds if game.speak_seconds else None
            bot_skill(game, speech)
            ended = outcome = None
            try:
                while speaker.state["is_alive"]:
                    waiters = {asyncio.ensure_future(view.event.wait()), asyncio.ensure_future(speech.queue.get())}
                    try:
                        done, _ = await asyncio.wait(
                            waiters, timeout=None if deadline is None else max(0, deadline - loop.time()),
                            return_when=asyncio.FIRST_COMPLETED
                        )
                    finally:  # also when the game is cancelled mid-speech
                        for task in waiters:
                            if not task.done():
                                task.cancel()
                    if not done or view.event.is_set():
                        break  # timeout or skip
                    request = next(iter(done)).result()
                    ended, outcome = await handle_skill(game, request)
                    if ended or outcome:
                        break
                    if speaker.state["is_alive"]:
                        await lock_voice(game, {speaker.id})  # last words muted them again
            finally:
                view.stop()
                await lock_voice(game, set())  # muted again at once, on skip, timeout or cancel
            await msg.edit(content=f"🎤 {name} 發言結束。", view=None)
            if ended or outcome:
                speech.open = False
                return bool(ended), outcome
    finally:
        speech.open = False
    return False, None


# ---------------------------------------------------------------- exile vote
def vote_report(game: Game, votes: dict[int, int | None], detail: dict, weights: dict | None = None,
                compact: bool = False) -> str:
    """The vote result: each player with their votes, then the voters one per line below (no brackets).
    `compact` is the one-line form for the game record."""
    weights = weights or {}
    bonus = detail.get("bonus") or {}
    by_target: dict[int, list[int]] = {}
    abstain: list[int] = []
    for voter, target in votes.items():
        if target is None:
            abstain.append(voter)
        else:
            by_target.setdefault(target, []).append(voter)

    def who(voters: list[int]) -> str:
        return "、".join(f"{game.players[v].seat}號" + ("🎖️" if v in weights else "") for v in voters)

    def total(voters: list[int]) -> str:
        return f"{sum(weights.get(v, 1) for v in voters):g}"

    for t in bonus:  # a cursed player is listed even if nobody voted for them
        by_target.setdefault(t, [])

    def score(t: int) -> float:
        return sum(weights.get(v, 1) for v in by_target[t]) + bonus.get(t, 0)

    def names(voters: list[int]) -> list[str]:  # one voter per line: "1號 假人1"
        ordered = sorted(voters, key=lambda v: game.players[v].seat)
        return [f"　{seat_label(game, v)}" + ("🎖" if v in weights else "") for v in ordered] or ["　無人投票"]

    curse = lambda t: f"　🐦 烏鴉詛咒 +{bonus[t]:g}" if t in bonus else ""  # noqa: E731
    lines: list[str] = []
    for t in sorted(by_target, key=lambda t: -score(t)):
        if compact:
            lines.append(f"• {seat_label(game, t)}：{score(t):g} 票（{who(by_target[t]) or '無人投票'}）{curse(t)}")
        else:
            lines += [f"• {seat_label(game, t)}：{score(t):g} 票{curse(t)}"] + names(by_target[t])
    if abstain:
        if compact:
            lines.append(f"• 棄權：{total(abstain)} 票（{who(abstain)}）")
        else:
            lines += [f"• 棄權：{total(abstain)} 票"] + names(abstain)
    return "\n".join(lines) or "沒有人投票。"


async def ballot(game: Game, opening: str, candidates: list[int] | None = None,
                 exclude: set[int] = frozenset(), cursed: bool = False) -> tuple[int | None, dict]:
    """One round of voting. `candidates` limits who can be voted for (the tied players in a 平票PK),
    `exclude` are players who may not vote (the tied players themselves)."""
    state = N.NightState(game.night_number)
    if candidates is not None:
        state.extra["vote_candidates"] = set(candidates)
    voters = [p for p in D.voters(game) if p.id not in exclude]
    turn = N.Turn("vote", "投票", "🗳", "day_vote", 0, config.DAY_VOTE_SECONDS, voters)
    await collect(game, turn, state, opening, "🗳 投票結束。")
    votes = state.acts.get("day_vote", {})
    valid = candidates if candidates is not None else [
        p.id for p in N.alive(game) if p.state.get("can_be_voted", True)]
    game.last_vote_record = dict(votes)  # the bomber needs to know who voted for him
    weights = sheriff_weights(game)  # the living sheriff's vote counts 1.5
    extra = N.settings(game)["crow_extra_votes"]
    bonus = {t: extra for t in game.cursed if game.players[t].state["is_alive"]} if cursed else None
    exiled, detail = D.tally_exile(votes, valid, weights, bonus)
    await say(game, ("📊 **投票結果**\n" + vote_report(game, votes, detail, weights))[:1900])
    record = vote_report(game, votes, detail, weights, compact=True)
    game.history.append(f"第{game.night_number}天投票：" + record.replace("• ", "").replace("\n", "；"))
    return exiled, detail


async def run_vote(game: Game) -> tuple[list[int], bool, Outcome | None]:
    """The exile vote, with a 平票PK when the first place is tied.

    平票PK: the tied players each speak once more, then everyone else (the tied players cannot
    vote) votes again, only among them. A second tie exiles nobody.
    Returns (who gives last words, the day ended suddenly, the outcome if the game is decided)."""
    exiled, detail = await ballot(
        game, "🗳 **放逐投票**開始！請按「行動」選擇要放逐的玩家（可棄權）。", cursed=True)
    if exiled is not None or not detail["tied"]:
        game.consecutive_ties = 0  # only days that end in a tie with nobody exiled count
    if exiled is None and detail["tied"]:
        tied = detail["tied"]
        names = "、".join(seat_label(game, pid) for pid in tied)
        await say(game, f"⚖️ **平票！** {names} 同票，進入**平票PK**：他們各再發言一輪，"
                        "之後由其他玩家在他們之中重新投票（平票者不能投票）。")
        game.history.append(f"第{game.night_number}天平票PK：{names}")
        order = D.speaking_order(game, only=set(tied))
        ended, outcome = await run_speeches(game, order, "🎤 平票PK發言順序：")
        if ended or outcome:  # a skill during the PK: sudden night, or the game is over
            return [], ended, outcome
        candidates = [pid for pid in tied if game.players[pid].state["is_alive"]]
        exiled, _ = await ballot(
            game, "🗳 **平票PK投票**！請按「行動」，在平票的玩家之中選擇要放逐的人（可棄權）。",
            candidates=candidates, exclude=set(tied))
        if exiled is not None:
            game.consecutive_ties = 0
        else:
            await say(game, "⚖️ 平票PK後仍然沒有結果，沒有人被放逐。")
            game.history.append("平票PK後無人被放逐。")
            game.consecutive_ties += 1
            need = N.settings(game)["tie_draw_days"]
            if need > 0 and game.consecutive_ties >= need and not N.night_kill_possible(game):
                text = f"連續 {game.consecutive_ties} 天平票無人出局，且場上已沒有任何夜晚擊殺能力"
                game.history.append(text)
                return [], False, Outcome("DRAW", text)
            return [], False, None
    return await exile(game, exiled), False, None


async def exile(game: Game, exiled: int | None) -> list[int]:
    """Carry out the exile. Returns who gives last words: the exiled player and anyone their shot took."""
    if exiled is None:
        await say(game, "⚖ 沒有人被放逐。")
        return []
    player = game.players[exiled]
    ability = D.exile_cancel_ability(game, player)
    if ability:
        D.reveal_survivor(game, player, ability)
        await say(game, f"🃏 **{seat_label(game, exiled)}** 是 **{game.role_of(player)['name']}**！免於放逐，"
                        "之後不能投票（但仍可發言，再被放逐就會出局）。")
        game.history.append(f"{seat_label(game, exiled)} 翻牌免於放逐。")
        return []
    game.last_exiled = exiled
    if D.exile_is_delayed(game, player):
        game.delayed_deaths.append((exiled, "VOTE_EXILE"))
        await say(game, f"🩸 **{seat_label(game, exiled)}** 被放逐，但他是最後的狼人，撐到下一個白天才會死亡。")
        game.history.append(f"{seat_label(game, exiled)} 被放逐（延遲死亡）。")
        return []  # not dead yet: the last words come when they die, at the next dawn
    deaths = apply_deaths(game, [(exiled, "VOTE_EXILE")])
    if player.state.get("win_condition") == "BE_EXILED":  # the jester got what they wanted
        game.jester_winners.append(exiled)
        game.history.append(f"{seat_label(game, exiled)}（小丑）被放逐，達成個人勝利。")
        await say(game, f"🃏 **{seat_label(game, exiled)}** 是 **{game.role_of(player)['name']}**！被公投出局，小丑獲勝！")
    names = "、".join(seat_label(game, pid) for pid, _ in deaths)
    await say(game, f"⚖️ **{seat_label(game, exiled)}** 被放逐。" + (f"（連帶死亡：{names}）" if len(deaths) > 1 else ""))
    game.history.append(f"{names} 死於放逐。")
    await bomber_explodes(game, player, exiled)
    extra = await shots_for(game, deaths, f"第{game.night_number}天：")
    await settle_badge(game)
    return [exiled] + [pid for pid, _ in extra]  # a lover who followed them dies without last words


async def bomber_explodes(game: Game, player: Player, exiled: int) -> None:
    """The bomber, exiled by the vote, kills everyone who voted for them (death_trigger KILL_VOTERS).
    If that is everybody still alive, the bomber wins alone."""
    trig = (game.role_of(player) or {}).get("death_trigger")
    if not trig or trig["effect"]["type"] != "KILL_VOTERS" or "VOTE_EXILE" not in trig["allowed_causes"]:
        return
    victims = [v for v, t in game.last_vote_record.items() if t == exiled and game.players[v].state["is_alive"]]
    if not victims:
        await say(game, f"💣 **{seat_label(game, exiled)}** 是炸彈人！但沒有人投給他，沒有炸死任何人。")
        game.history.append("炸彈人被放逐，沒有炸到人。")
        return
    deaths = apply_deaths(game, [(v, trig["effect"]["cause"]) for v in victims])
    names = "、".join(seat_label(game, pid) for pid, _ in deaths)
    await say(game, f"💣 **{seat_label(game, exiled)}** 是炸彈人！爆炸炸死了：{names}")
    game.history.append(f"炸彈人爆炸，炸死：{names}")
    if not N.alive(game):
        game.bomber_win = True


async def growl(game: Game) -> None:
    """The bear trainer, announced after the deaths: 熊咆哮了 (a wolf next to the bear), 熊很安靜, or once the
    bear is dead 熊不再咆哮了 (and nothing more after that). The bear is not named. 'Wolf' means what the
    seer would see. A bear who was feared or sealed, or a feared wolf next to it, does not growl.
    Next to the bear means the real seats after the magician's swap."""
    swap = game.night.swap if game.night else {}
    for bear in sorted(game.players.values(), key=lambda p: p.seat):
        role = game.role_of(bear)
        if not role or not any(a["trigger"] == "DAWN" and any(e["type"] == "CHECK_NEIGHBORS_FOR_WOLF" for e in a["effects"])
                               for a in role["abilities"]):
            continue
        if bear.id in game.bear_gone:
            continue
        if not bear.state["is_alive"]:
            game.bear_gone.add(bear.id)
            await say(game, "🐻 **熊不再咆哮了！**")
            game.history.append(f"第{game.night_number}天：熊（{bear.seat}號）已死亡，不再咆哮。")
            continue
        blocked = bear.id in game.feared_last_night or bool(game.night and game.night.sealed)
        near = N.bear_neighbors(game, bear, swap)
        roars = not blocked and any(
            p.state.get("seer_result") == "WEREWOLF" and p.id not in game.feared_last_night for p in near)
        await say(game, "🐻 **熊咆哮了！**" if roars else "🐻 **熊很安靜。**")
        game.history.append(f"第{game.night_number}天：熊{'咆哮' if roars else '很安靜'}（{bear.seat}號）。")


async def dusk(game: Game) -> Outcome | None:
    """Night falls: whoever was only wounded (old rogue) now dies. Returns the outcome if that decides the game."""
    pending, game.dusk_deaths = game.dusk_deaths, []
    pending = [(pid, cause) for pid, cause in pending if game.players[pid].state["is_alive"]]
    if not pending:
        return None
    deaths = apply_deaths(game, pending, force=True)
    names = "、".join(seat_label(game, pid) for pid, _ in deaths)
    await say(game, f"🌙 天黑了。**{names}** 傷重不治，死亡。")
    game.history.append(f"第{game.night_number}天黑：{names} 傷重死亡。")
    await settle_badge(game)
    return evaluate(game)


# ---------------------------------------------------------------- the day
async def announce_effects(game: Game) -> None:
    """The silenced player must be named (their turn will be skipped). The curse stays secret unless
    the rule 'crow_curse_public' is on."""
    if game.silenced:
        names = "、".join(seat_label(game, i) for i in sorted(game.silenced, key=lambda i: game.players[i].seat))
        await say(game, f"🤐 **禁言**：{names} 今天不能發言（輪到時會跳過），也不能用手勢、眼神或暗號暗示身分。仍可投票。")
        game.history.append(f"第{game.night_number}天禁言：{names}")
    if game.cursed:
        names = "、".join(seat_label(game, i) for i in sorted(game.cursed, key=lambda i: game.players[i].seat))
        game.history.append(f"第{game.night_number}天烏鴉詛咒：{names}")
        if N.settings(game)["crow_curse_public"]:
            await say(game, f"🐦 **烏鴉標記**：{names} 今天在放逐投票會多 {N.settings(game)['crow_extra_votes']} 票。")


async def run_day(game: Game, night_result: N.NightResult) -> Outcome | None:
    """Play one day. Returns the outcome as soon as the game is decided, else None.
    Win checks run after the night's deaths and shots, after each skill, and after the exile vote.
    A game that is already decided gets no last words."""
    game.last_exiled = None
    await move_wolves(game, False)  # make sure every wolf is back before the day
    game.voice_phase = "day"
    await gather_players(game)  # everyone returns to the main voice channel, muted
    await lock_voice(game, set())
    game.last_wolf_night = False
    # 警長競選 comes first on the first day, BEFORE the night's deaths are announced: nobody knows yet who
    # died, so the night's dead still take part (they are treated as alive for the election only).
    election_today = (game.sheriff_enabled and game.sheriff is None and not game.badge_torn
                      and (game.night_number == 1 or game.election_retry) and evaluate(game) is None)
    ended = False
    if election_today:
        await say(game, "☀ **天亮了。** 先進行警長競選，競選結束後才公布昨夜的死訊。")
        hidden = [pid for pid, _ in night_result.deaths]
        game.election_hidden = set(hidden)
        for pid in hidden:
            game.players[pid].state["is_alive"] = True
        game.election_retry = False
        try:
            ended, _ = await run_election(game)  # the real outcome is checked below, with the dead dead again
        finally:
            game.election_hidden = set()
            for pid in hidden:
                game.players[pid].state["is_alive"] = False
    speakers = await dawn(game, night_result, greeted=election_today)
    await announce_effects(game)
    await refresh_cards(game)
    outcome = evaluate(game)
    if outcome:
        return outcome
    await growl(game)
    if ended:  # a wolf blew up during the election: the news is out, straight to night, no last words
        return None
    await last_words(game, speakers)
    order = await speaking_order_today(game)  # the sheriff's direction (None: no sheriff, random start)
    ended, outcome = await run_speeches(
        game, order, "🎤 發言順序：" if order is None else "🎤 發言順序（警長最後發言）：")
    if outcome:
        return outcome
    if ended:  # a self-destruct or a winning duel sends everyone straight to night, no last words
        return None
    speakers, ended, outcome = await run_vote(game)
    await refresh_cards(game)
    outcome = outcome or evaluate(game)
    if outcome:
        return outcome
    if ended:  # a self-destruct during the 平票PK: straight to night, no last words
        return None
    await last_words(game, speakers)
    return None
