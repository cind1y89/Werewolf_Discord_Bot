"""One place where players die. Handles links (lovers, wolf beauty's charm, dream catcher)
and the wild child turning into a wolf when the role model dies. No Discord code."""
from __future__ import annotations

from .models import Game, Player


# A link only fires for some causes of the source's death (data: the wolf beauty's charm needs an
# exile or a hunter's shot; a poisoned wolf beauty does not take the charmed player with her).
LINK_ONLY_CAUSES = {"WOLF_BEAUTY_LINK": {"VOTE_EXILE", "HUNTER_SHOT", "WOLF_KING_SHOT"}}


def delays_death(game: Game, player: Player, cause: str) -> bool:
    """The old rogue: some causes do not kill at once, the player dies when night falls."""
    role = game.role_of(player)
    for ab in (role or {}).get("abilities", []):
        for effect in ab["effects"]:
            if effect["type"] == "DELAY_DEATH_UNTIL_NIGHT" and cause in effect.get("causes", []):
                return True
    return False


def kill(game: Game, player_id: int) -> None:
    game.players[player_id].state["is_alive"] = False


def apply_deaths(
    game: Game,
    events: list[tuple[int, str]],
    extra_links: list[tuple[int, int, str]] | None = None,
    force: bool = False,
) -> list[tuple[int, list[str]]]:
    """Kill every (player, cause) in `events` who is alive, then everyone linked to them,
    again and again until no new deaths. Returns [(player id, [causes])] in death order.
    A player who dies twice in the same call keeps both causes. A death the role delays (old rogue)
    is parked in game.dusk_deaths and does not appear here, unless `force` (it is time to die)."""
    links = game.links + (extra_links or [])
    deaths: dict[int, list[str]] = {}
    queue: list[int] = []
    for pid, cause in events:
        player = game.players[pid]
        if player.state["is_alive"] and not force and delays_death(game, player, cause):
            if all(p != pid for p, _ in game.dusk_deaths):
                game.dusk_deaths.append((pid, cause))
        elif player.state["is_alive"]:
            kill(game, pid)
            deaths[pid] = [cause]
            queue.append(pid)
        elif pid in deaths and cause not in deaths[pid]:
            deaths[pid].append(cause)  # a second cause in the same resolution
    while queue:
        src = queue.pop(0)
        for source, target, cause in links:
            if source == src and game.players[target].state["is_alive"]:
                only = LINK_ONLY_CAUSES.get(cause)
                if only and not (only & set(deaths[src])):
                    continue
                kill(game, target)
                deaths[target] = [cause]
                queue.append(target)
    react_to_deaths(game, deaths)
    return list(deaths.items())


def react_to_deaths(game: Game, deaths: dict[int, list[str]]) -> None:
    """Wild child: when the role model dies, become a wolf (data: role dynamic_rules ROLE_MODEL_DIED,
    optionally only for some causes: `only_causes`)."""
    dead = set(deaths)
    for player in game.players.values():
        if not player.state["is_alive"]:
            continue
        model = player.state["custom_state"].get("role_model_id")
        if model is None or model not in dead:
            continue
        for rule in game.role_of(player).get("dynamic_rules", []):
            if rule["trigger"] != "ROLE_MODEL_DIED":
                continue
            only = rule.get("only_causes")
            if only and not (set(only) & set(deaths[model])):
                continue  # the model died some other way: the wild child stays good
            for effect in rule["effects"]:
                if effect["type"] == "CHANGE_FACTION":
                    player.state["faction"] = effect["faction"]
                elif effect["type"] == "SET_WIN_CONDITION":
                    player.state["win_condition"] = effect["win_condition"]
                elif effect["type"] == "SET_SEER_RESULT":
                    player.state["seer_result"] = effect["seer_result"]
                elif effect["type"] == "GRANT_ABILITY":
                    player.state["custom_state"]["joined_pack"] = True
            player.state["custom_state"]["role_model_id"] = None  # change happens only once
