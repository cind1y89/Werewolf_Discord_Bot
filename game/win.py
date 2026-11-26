"""Win rules. Pure logic, no Discord code.

Checked after every death resolution: after the night, after hunter shots, after the exile vote.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import night as N
from .models import Game, Player

# What each werewolf-win target from data/rules/victory_modes.json means.
SIDE_TARGETS = {"ALL_CITIZENS_ELIMINATED": "CITIZEN", "ALL_GODS_ELIMINATED": "GOD"}
CITY_TARGET = "ALL_VILLAGERS_ELIMINATED"


@dataclass
class Outcome:
    winner: str  # "VILLAGER", "WEREWOLF", "THIRD_PARTY", "JESTER", "BOMBER" or "DRAW"
    reason: str


def _category(game: Game, player: Player) -> str | None:
    role = game.role_of(player)
    return role["initial_state"]["category"] if role else None


def evaluate(game: Game) -> Outcome | None:
    """Return the outcome if the game is over, else None.

    - Good side wins when every wolf is dead.
    - Wolves win by the host's victory rule. 屠邊: all citizens or all gods dead (a group that was
      never in the game is ignored; if neither was, it behaves like 屠城). 屠城: all good players dead.
    - If both sides' conditions are met at once (or nobody is left), it is a draw."""
    alive = N.alive(game)

    if game.bomber_win:
        return Outcome("BOMBER", "炸彈人炸死了所有存活的玩家")
    # The jester's win is personal and normally does not stop the game; the board can make it end the game.
    if game.jester_winners and N.settings(game)["jester_win_ends_game"]:
        return Outcome("JESTER", "小丑被公投出局")

    # Third-party lovers (one wolf, one good): while both lovers live, neither side can win
    # unless everyone else is gone, in which case the lovers and cupid win together.
    if game.lovers_mixed and game.lovers and all(game.players[i].state["is_alive"] for i in game.lovers):
        allies = {*game.lovers, game.cupid_id}
        if all(p.id in allies for p in alive):
            return Outcome("THIRD_PARTY", "情侶與邱比特存活到最後")
        return None

    wolves = [p for p in alive if p.state.get("faction") == "WEREWOLF"]
    good = [p for p in alive if p.state.get("faction") == "VILLAGER"]

    wolf_targets = game.wolf_targets()
    wolves_win_reason = None
    side_groups = [SIDE_TARGETS[t] for t in wolf_targets if t in SIDE_TARGETS]
    in_game = {_category(game, p) for p in game.players.values()}
    for group in side_groups:
        if group in in_game and not any(_category(game, p) == group for p in alive):
            wolves_win_reason = "狼人屠光了所有平民" if group == "CITIZEN" else "狼人屠光了所有神職"
            break
    if CITY_TARGET in wolf_targets or not any(g in in_game for g in side_groups):
        if not good:
            wolves_win_reason = "狼人屠光了所有好人"

    wolves_dead = not wolves
    if not alive:
        return Outcome("DRAW", "場上已無人存活")
    # A third party that must be dealt with first (the bomber): while one is alive, neither side has won.
    blockers = [p for p in alive if N.has_tag(game, p, "blocks_victory")]
    if blockers:
        if all(p in blockers for p in alive):
            return Outcome("BOMBER", "只剩炸彈人存活")
        return None
    if wolves_dead and wolves_win_reason:
        if game.last_wolf_night:  # the blood moon apostle's last night: the wolves' kill decides
            return Outcome("WEREWOLF", "血月使徒的最後一夜，好人陣營被屠邊")
        return Outcome("DRAW", "雙方勝利條件同時達成")
    if wolves_dead and not good:
        return Outcome("DRAW", "好人與狼人都已出局")
    if wolves_dead:
        return Outcome("VILLAGER", "所有狼人都已出局")
    if wolves_win_reason:
        return Outcome("WEREWOLF", wolves_win_reason)
    return None
