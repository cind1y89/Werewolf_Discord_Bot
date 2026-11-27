"""Day rules: speaking order, the banishment vote, death triggers, the idiot.

No Discord code here, so it can be tested on its own. The Discord side is game/day_flow.py.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import night as N
from .deaths import apply_deaths
from .models import Game, Player


# ---------------------------------------------------------------- speaking
def speaking_order(game: Game, rng: random.Random | None = None, only: set[int] | None = None) -> list[Player]:
    """Living players clockwise (ascending seat), starting at a random living seat.
    `only` limits it to some players (the 平票PK speakers)."""
    rng = rng or random
    living = sorted((p for p in N.alive(game) if only is None or p.id in only), key=lambda p: p.seat)
    if not living:
        return []
    start = rng.randrange(len(living))
    return living[start:] + living[:start]


# ---------------------------------------------------------------- voting
def voters(game: Game) -> list[Player]:
    return [p for p in N.alive(game) if p.state.get("can_vote", True)]


def vote_options(game: Game, voter: Player, only: set[int] | None = None) -> list[Player]:
    """Who this voter may vote for: living, allowed to be voted, not themselves.
    In a 平票PK vote `only` limits it to the tied players."""
    return [p for p in N.alive(game)
            if p.state.get("can_be_voted", True) and p.id != voter.id and (only is None or p.id in only)]


def tally_exile(votes: dict[int, int | None], valid_ids: list[int],
                weights: dict[int, float] | None = None,
                bonus: dict[int, float] | None = None) -> tuple[int | None, dict]:
    """Returns (exiled player id or None, counts).

    Self-votes are ignored. Abstain votes are not looked at. Only the votes for players count: the player
    with the most votes is exiled; if two or more players share the most votes (any number of them),
    detail["tied"] lists them and that is the 平票PK case; with no votes for anyone, nobody is exiled.
    `bonus` adds extra votes to chosen players (the crow's curse)."""
    weights = weights or {}  # the sheriff's vote counts 1.5, everyone else 1
    counts: dict[int, float] = {}
    abstain = 0.0
    for voter, target in votes.items():
        weight = weights.get(voter, 1)
        if target is None:
            abstain += weight
        elif target == voter:
            continue  # nobody can vote for themselves; such a vote is not counted
        elif target in valid_ids:
            counts[target] = counts.get(target, 0) + weight
    for target, extra in (bonus or {}).items():  # the crow's curse: extra votes for the cursed player
        if target in valid_ids:
            counts[target] = counts.get(target, 0) + extra
    detail = {"counts": counts, "abstain": abstain, "tied": [], "bonus": dict(bonus or {})}
    if not counts:
        return None, detail
    top = max(counts.values())
    leaders = [t for t, c in counts.items() if c == top]
    if len(leaders) > 1:
        detail["tied"] = leaders  # a tie for first place between players: 平票PK, however many are tied
        return None, detail
    return leaders[0], detail


# ---------------------------------------------------------------- death triggers
def can_shoot(game: Game, player: Player, causes: list[str]) -> bool:
    """Does this death trigger the role's death_trigger? Reads allowed/forbidden causes from the JSON."""
    role = game.role_of(player)
    trig = role.get("death_trigger") if role else None
    if not trig or trig["effect"]["type"] != "KILL_TARGET" or N.resource_left(player, "bullet") <= 0:
        return False
    if any(c in trig["forbidden_causes"] for c in causes):
        return False
    return any(c in trig["allowed_causes"] for c in causes)


def why_no_shot(game: Game, player: Player, causes: list[str]) -> str | None:
    """For a role with a death shot that cannot shoot after this death: the reason, else None."""
    role = game.role_of(player)
    trig = role.get("death_trigger") if role else None
    if not trig or trig["effect"]["type"] != "KILL_TARGET" or can_shoot(game, player, causes):
        return None
    if N.resource_left(player, "bullet") <= 0:
        return "你的子彈已經用完了。"
    died = "、".join(N.CAUSE_NAMES.get(c, c) for c in causes)
    allowed = "、".join(N.CAUSE_NAMES.get(c, c) for c in trig["allowed_causes"])
    return f"你死於「{died}」。這個技能只有在「{allowed}」時才能發動。"


def has_death_shot(game: Game, player: Player) -> bool:
    role = game.role_of(player)
    trig = role.get("death_trigger") if role else None
    return bool(trig and trig["effect"]["type"] == "KILL_TARGET")


def death_triggers(game: Game, deaths: list[tuple[int, list[str]]]) -> list[Player]:
    """Dead players who now get a death shot."""
    return [game.players[pid] for pid, causes in deaths if can_shoot(game, game.players[pid], causes)]


async def resolve_shots(game: Game, deaths: list[tuple[int, list[str]]], choose) -> list[tuple[int, list[str]]]:
    """Run death triggers, including chains, and return every extra death.

    `await choose(shooter)` returns the target id or None. Each player triggers at most once,
    so chains cannot loop. The cause of the shot (HUNTER_SHOT, WOLF_KING_SHOT) comes from the
    role's death_trigger, and whether a shot victim can shoot back is decided by the JSON."""
    extra: list[tuple[int, list[str]]] = []
    done: set[int] = set()
    queue = list(deaths)
    while queue:
        pid, causes = queue.pop(0)
        shooter = game.players[pid]
        if pid in done or not can_shoot(game, shooter, causes):
            continue
        done.add(pid)
        target = await choose(shooter)
        if target is None or not game.players[target].state["is_alive"]:
            continue
        N.use_resource(shooter, "bullet")
        cause = game.role_of(shooter)["death_trigger"]["effect"]["cause"]
        new = apply_deaths(game, [(target, cause)])  # includes linked deaths (lovers, charm)
        extra += new
        queue += new
    return extra


# ---------------------------------------------------------------- idiot
def exile_cancel_ability(game: Game, player: Player) -> dict | None:
    """The role's ability that cancels an exile (the idiot), if it is still available."""
    role = game.role_of(player)
    if not role or player.state.get("is_revealed"):
        return None
    for ability in role["abilities"]:
        if ability["trigger"] == "DAY_VOTE" and any(e["type"] == "CANCEL_EXILE" for e in ability["effects"]):
            return ability
    return None


def reveal_survivor(game: Game, player: Player, ability: dict) -> None:
    """Apply the ability's effects: revealed, and (for the idiot) no more voting rights."""
    player.state["is_revealed"] = True
    for effect in ability["effects"]:
        if effect["type"] == "SET_VOTE_RIGHTS":
            player.state["can_vote"] = effect["can_vote"]
            player.state["can_be_voted"] = effect["can_be_voted"]


# ---------------------------------------------------------------- day skills
@dataclass
class DayEvent:
    text: str
    deaths: list[tuple[int, list[str]]]
    ends_day: bool  # True: skip the rest of the day and go straight to night


def day_skills(game: Game, player: Player) -> list[str]:
    """Skill ids this player can use during the speeches right now (from the role's JSON)."""
    if not player.state.get("is_alive") or custom_used(player, "day_skill_used"):
        return []
    role = game.role_of(player)
    if role is None:
        return []
    skills = [a["id"] for a in role["abilities"] if a["trigger"] == "DAY_SPEECH"]
    lib = game.data["ability_library"]
    skills += [r for r in role["ability_refs"] if r in lib and lib[r]["trigger"] == "DAY_SPEECH"]
    return skills


def no_day_skill_text(game: Game, player: Player | None) -> str:
    """Why 發動技能 has nothing to offer, without implying the role has no skill at all."""
    if player is None or game.role_of(player) is None:
        return "你不在這局遊戲中。"
    if not player.state.get("is_alive"):
        return "你已經出局了。"
    role = game.role_of(player)
    if custom_used(player, "day_skill_used"):
        return f"你的【{role['name']}】白天技能已經用過了。"
    night = role["actions"]["night"] or any(a["trigger"] in ("NIGHT", "FIRST_NIGHT") for a in role["abilities"])
    if night:
        return (f"你的【{role['name']}】技能是在夜晚使用的，天黑後請到你自己的頻道按「行動」。\n"
                "白天沒有可以發動的技能。")
    return f"【{role['name']}】沒有白天可以發動的技能。"


def custom_used(player: Player, key: str) -> bool:
    return bool(player.state["custom_state"].get(key))


SKILL_NAMES = {
    "wolf_suicide": "自爆", "white_wolf_explode_kill": "自爆並帶走一人",
    "blood_moon_explode_seal": "自爆並封印神職", "day_duel_action": "翻牌決鬥",
}
SKILLS_NEEDING_TARGET = {"white_wolf_explode_kill", "day_duel_action"}


def use_day_skill(game: Game, player: Player, skill: str, target: int | None = None) -> DayEvent:
    """Carry out a day skill. The caller announces the result, then runs death shots."""
    name = N.label(game, player.id)
    player.state["custom_state"]["day_skill_used"] = True
    if skill == "day_duel_action":
        player.state["is_revealed"] = True
        target_p = game.players[target]
        if target_p.state["faction"] == "WEREWOLF":
            deaths = apply_deaths(game, [(target, "KNIGHT_DUEL")])
            return DayEvent(f"⚔️ **{name}** 翻牌是騎士！決鬥對象 {N.label(game, target)} 是狼人，狼人出局，直接進入黑夜。",
                            deaths, True)
        deaths = apply_deaths(game, [(player.id, "KNIGHT_DUEL")])
        return DayEvent(f"⚔️ **{name}** 翻牌是騎士！決鬥對象 {N.label(game, target)} 是好人，騎士以死謝罪，白天繼續。",
                        deaths, False)
    events = [(player.id, "SUICIDE")]
    text = f"💥 **{name}** 自爆了！直接進入黑夜。"
    if skill == "white_wolf_explode_kill":
        events.append((target, "WHITE_WOLF_KING_EXPLODE"))
        text = f"💥 **{name}** 是白狼王，自爆並帶走了 {N.label(game, target)}！直接進入黑夜。"
    elif skill == "blood_moon_explode_seal":
        game.sealed_tonight = True
        text = f"🩸 **{name}** 是血月使徒，自爆！今晚好人神職的技能被封印，直接進入黑夜。"
    return DayEvent(text, apply_deaths(game, events), True)


def skill_targets(game: Game, player: Player) -> list[Player]:
    return [p for p in N.alive(game) if p.id != player.id]


# ---------------------------------------------------------------- delayed exile
def exile_is_delayed(game: Game, player: Player) -> bool:
    """Blood moon apostle: if it is the last wolf, the exile does not kill it until the next day."""
    role = game.role_of(player)
    if not role:
        return False
    others = [p for p in N.alive(game) if p.state["faction"] == "WEREWOLF" and p.id != player.id]
    if others:
        return False
    return any(
        rule["trigger"] == "LAST_WOLF_STANDING"
        and any(e["type"] == "DELAY_VOTE_EXILE_DEATH" for e in rule["effects"])
        for rule in role.get("dynamic_rules", [])
    )


def process_delayed_deaths(game: Game) -> list[tuple[int, list[str]]]:
    """Deaths postponed from yesterday's exile happen now, at the start of the day."""
    pending, game.delayed_deaths = game.delayed_deaths, []
    game.last_wolf_night = bool(pending)  # the blood moon apostle's last night is over
    return apply_deaths(game, pending)
