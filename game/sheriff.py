"""Sheriff (警長) rules. Pure logic, no Discord code.

The election runs after the first night. Players choose 上警 (run) or 不上警. The candidates
speak and may 退水 (withdraw); everyone not running, including those who withdrew, votes among
the rest. A tie means the tied candidates speak once more and the others vote again; a second
tie means no sheriff. The sheriff's exile vote counts 1.5 (rule_set: sheriff_vote_weight), and
the sheriff picks the direction of the day's speeches and speaks last. A dead sheriff hands the
badge to a living player or tears it up."""
from __future__ import annotations

import random

from . import night as N
from .models import Game, Player


def is_sheriff_alive(game: Game) -> bool:
    return game.sheriff is not None and game.players[game.sheriff].state.get("is_alive", False)


def weights(game: Game) -> dict[int, float]:
    """Vote weights for the exile vote: only the living sheriff counts more than 1."""
    if is_sheriff_alive(game):
        return {game.sheriff: N.settings(game)["sheriff_vote_weight"]}
    return {}


def tally_sheriff(votes: dict[int, int | None], candidates: list[int]) -> tuple[int | None, list[int], dict]:
    """The most-voted candidate wins. Returns (winner or None, tied candidates, counts).
    Abstaining and self-votes do not count. No votes at all, or a tie, gives no winner."""
    counts: dict[int, int] = {}
    for voter, target in votes.items():
        if target is not None and target != voter and target in candidates:
            counts[target] = counts.get(target, 0) + 1
    if not counts:
        return None, [], counts
    top = max(counts.values())
    leaders = [t for t, c in counts.items() if c == top]
    return (leaders[0], [], counts) if len(leaders) == 1 else (None, leaders, counts)


def sheriff_order(game: Game, direction: str) -> list[Player]:
    """The day's speaking order the sheriff chose: start next to the sheriff and go round in the chosen
    direction ('up' = seat numbers rising, 'down' = falling); the sheriff speaks last."""
    living = sorted(N.alive(game), key=lambda p: p.seat)
    me = next(i for i, p in enumerate(living) if p.id == game.sheriff)
    ring = living[me + 1:] + living[:me]  # everyone else, going up from the sheriff
    return (ring if direction == "up" else ring[::-1]) + [living[me]]


def random_direction(rng: random.Random | None = None) -> str:
    return (rng or random).choice(("up", "down"))
