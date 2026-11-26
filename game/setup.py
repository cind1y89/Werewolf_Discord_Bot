from __future__ import annotations

import random

from .models import Game


def build_role_pool(roles: list[str], fill_role: str, count: int) -> list[str]:
    """Role ids for `count` players. Fewer players take the first N roles of the
    list (so order matters); extra players get fill_role."""
    if count <= len(roles):
        return list(roles[:count])
    return list(roles) + [fill_role] * (count - len(roles))


def assign_seats(game: Game) -> None:
    """Random seats 1..N with two-way lookup."""
    seats = list(range(1, len(game.players) + 1))
    random.shuffle(seats)
    game.seat_to_user.clear()
    game.user_to_seat.clear()
    for player, seat in zip(game.players.values(), seats):
        player.seat = seat
        game.seat_to_user[seat] = player.id
        game.user_to_seat[player.id] = seat


def deal_roles(game: Game) -> None:
    assign_seats(game)
    n = len(game.players)
    pool = build_role_pool(game.role_list, game.fill_role, n + game.extra_cards)
    random.shuffle(pool)
    for player, role_id in zip(game.players.values(), pool):
        game.give_role(player, role_id)
    game.bottom_cards = pool[n:]  # the thief's two cards (empty without a thief)
    game.is_started = True


# ---------------------------------------------------------------- choosing a board for a number of players
def boards_for(data: dict, players: int) -> dict[str, dict]:
    """The preset boards made for exactly this many players."""
    return {pid: p for pid, p in data["presets"].items() if p.get("players") == players}


def default_roles(players: int) -> list[str]:
    """A basic balanced set for a player count that has no ready-made board:
    about a third wolves, then gods in a fixed order, the rest plain villagers."""
    wolves = max(1, players // 3)
    gods = ["seer", "witch", "hunter", "guard", "idiot", "knight"][: min(6, (players - wolves) // 2)]
    return ["werewolf"] * wolves + gods + ["villager"] * (players - wolves - len(gods))


def describe_roles(data: dict, roles: list[str]) -> str:
    """'預言家、女巫、狼人 ×3、平民 ×4' (wolves are named last so the town reads first)."""
    counts: dict[str, int] = {}
    for rid in roles:
        counts[rid] = counts.get(rid, 0) + 1
    town = [r for r in counts if data["roles"][r]["initial_state"]["faction"] != "WEREWOLF"]
    wolves = [r for r in counts if data["roles"][r]["initial_state"]["faction"] == "WEREWOLF"]
    return "、".join(
        data["roles"][r]["name"] + (f" ×{counts[r]}" if counts[r] > 1 else "") for r in town + wolves
    )
