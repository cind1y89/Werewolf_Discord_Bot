"""Night rules: who acts and in what order, what choices they have, and how the night resolves.

No Discord code here, so it can be tested on its own. The Discord side is game/night_flow.py.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field

from .deaths import apply_deaths
from .models import Game, Player

CAUSE_NAMES = {
    "WOLF_ATTACK": "狼人襲擊", "WITCH_POISON": "毒藥", "VOTE_EXILE": "放逐", "HUNTER_SHOT": "獵人開槍",
    "WOLF_KING_SHOT": "狼王開槍", "LOVER_LINK": "殉情", "WOLF_BEAUTY_LINK": "狼美人魅惑殉情",
    "DREAM_CATCHER_EFFECT": "攝夢人效果", "WITCH_HUNTER_HUNT": "獵魔人狩獵", "KNIGHT_DUEL": "騎士決鬥",
    "SUICIDE": "自爆", "WHITE_WOLF_KING_EXPLODE": "白狼王自爆帶走", "NIGHTMARE_REFLECT": "惡靈騎士反傷",
    "MIRACLE_MERCHANT_BACKFIRE": "奇蹟商人反噬", "BOMBER_EXPLODE": "炸彈人爆炸",
}
ROLE_EMOJI = {
    "werewolf": "🐺", "seer": "🔮", "witch": "🧪", "guard": "🛡️", "cupid": "💘", "thief": "🦝",
    "wild_child": "🌱", "magician": "🎩", "mechanical_wolf": "🤖", "miracle_merchant": "🎁",
    "dream_catcher": "💤", "wolf_beauty": "💋", "witch_hunter": "⚔️", "gargoyle": "🗿",
    "gravekeeper": "⚰️", "psychic": "🔍", "nightmare": "😱", "crow": "🐦", "silencer": "🤐",
    "fortune_teller": "🧿", "bear_trainer": "🐻", "bomber": "💣",
}
# Abilities that have a panel. "use_poison" is part of the witch's panel.
PANEL_ABILITIES = {
    "wolf_pack_attack", "bind_lovers", "pick_bottom_card_role", "choose_role_model", "swap_two_players",
    "mimic_target_ability", "guard_protect", "grant_lucky_skill", "assign_sleepwalker", "charm_player",
    "use_antidote", "hunt_suspect", "exact_role_check", "check_faction", "check_previous_exiled_faction",
    "fear_player", "curse_player", "silence_player", "bind_three",
}
# The wolves open their eyes right after the nightmare and the first-night setup roles (thief, cupid, wild
# child, gargoyle); the other night roles follow. The magician's swap is applied to the wolves' target afterwards.
WOLF_PRIORITY = 9
MIN_NIGHT = {"hunt_suspect": 2, "check_previous_exiled_faction": 2}  # witch hunter and gravekeeper start on night 2
GIFTS = {"CHECK_FACTION": "查驗", "POISON": "毒藥", "PROTECT": "守護"}
LUCKY_ABILITY = {"CHECK_FACTION": "lucky_check", "POISON": "lucky_poison", "PROTECT": "lucky_protect"}  # gift -> turn ability
# Skills that follow the player a magician swapped: the target is replaced when it is recorded.
REDIRECTED = {
    "guard_protect", "check_faction", "use_poison", "assign_sleepwalker", "grant_lucky_skill",
    "lucky_check", "lucky_poison", "lucky_protect", "charm_player", "hunt_suspect",
    "exact_role_check", "mimic_target_ability", "curse_player", "silence_player", "bind_three",
}


@dataclass
class Turn:
    key: str
    name: str
    emoji: str
    ability_id: str
    priority: float
    seconds: int
    actors: list[Player]


@dataclass
class NightState:
    number: int
    acts: dict[str, dict[int, int | None]] = field(default_factory=dict)
    wolf_target: int | None = None
    log: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # answers that are not one target (pairs, gifts, ...)
    swap: dict[int, int] = field(default_factory=dict)  # magician: player -> player their skills hit
    lucky: dict[int, str] = field(default_factory=dict)  # miracle merchant: lucky player -> gift
    done: set[str] = field(default_factory=set)
    sealed: bool = False  # blood moon apostle sealed the gods tonight
    feared: set[int] = field(default_factory=set)  # the nightmare's targets: no skill tonight

    @property
    def wolf_votes(self) -> dict[int, int | None]:
        return self.acts.get("wolf_pack_attack", {})

    def redirect(self, target: int | None) -> int | None:
        return self.swap.get(target, target)

    def record(self, ability: str, actor_id: int, target: int | None) -> None:
        if ability in REDIRECTED:
            target = self.redirect(target)
        self.acts.setdefault(ability, {})[actor_id] = target

    def targets(self, ability: str) -> list[int]:
        return [t for t in self.acts.get(ability, {}).values() if t is not None]


@dataclass
class NightResult:
    deaths: list[tuple[int, list[str]]]
    log: list[str]


def alive(game: Game) -> list[Player]:
    return [p for p in game.players.values() if p.state.get("is_alive")]


def settings(game: Game) -> dict:
    """The rule settings for this game: rule_set.json, changed by the chosen board's own rules."""
    return {**game.data["rule_set"]["settings"], **game.rule_overrides}


def label(game: Game, player_id: int) -> str:
    p = game.players[player_id]
    return f"{p.seat}號 {p.member.display_name}"


def category(game: Game, player: Player) -> str | None:
    role = game.role_of(player)
    return role["initial_state"]["category"] if role else None


def custom(player: Player) -> dict:
    return player.state["custom_state"]


# ---------------------------------------------------------------- turns
def wolves_turn_actors(game: Game) -> list[Player]:
    """The pack. When every pack wolf is dead, the remaining solo wolves take over the attack."""
    pack = [p for p in game.players.values() if game.is_pack_wolf(p)]
    if any(p.state["is_alive"] for p in pack):
        return pack
    solo = [p for p in game.players.values() if category(game, p) == "SOLO_WOLF"]
    return solo if any(p.state["is_alive"] for p in solo) else []


def wolves_blocked(game: Game, state: NightState) -> bool:
    """A feared wolf takes the whole pack's attack away for the night."""
    return any(a.id in state.feared and a.state["is_alive"] for a in wolves_turn_actors(game))


def candidate_turns(game: Game, state: NightState) -> list[Turn]:
    turns: list[Turn] = []
    wolves = wolves_turn_actors(game)
    if wolves:
        turns.append(Turn("wolves", "狼人", "🐺", "wolf_pack_attack", WOLF_PRIORITY, game.wolf_seconds, wolves))

    players = list(game.players.values())
    copied = {custom(p).get("copied_role") for p in players if state.number > 1} - {None}
    for rid in {p.role_id for p in players} | copied:
        role = game.data["roles"][rid]
        for ab in role["abilities"]:
            if ab["trigger"] not in ("NIGHT", "FIRST_NIGHT") or ab["id"] not in PANEL_ABILITIES:
                continue
            if ab["trigger"] == "FIRST_NIGHT" and state.number != 1:
                continue
            if state.number < MIN_NIGHT.get(ab["id"], 1):
                continue
            actors = [p for p in players if p.role_id == rid]
            if ab.get("active_when") == "FIRST_NIGHT_OR_LAST_WOLF" and state.number > 1:
                if any(game.is_pack_wolf(w) and w.state["is_alive"] for w in players):
                    actors = []  # the gargoyle checks after the first night only once the pack is dead
            if ab["trigger"] == "NIGHT":
                actors += [p for p in players if p.role_id != rid and custom(p).get("copied_role") == rid
                           and state.number > 1]
            if actors:
                turns.append(Turn(f"{rid}:{ab['id']}", role["name"], ROLE_EMOJI.get(rid, "🎭"), ab["id"],
                                  ab["execution"]["priority"], game.night_seconds, actors))

    if state.extra.get("lovers_pending") and game.lovers:
        turns.append(Turn("lovers", "情侶", "💞", "lovers_info", 5.5, game.night_seconds,
                          [game.players[i] for i in game.lovers]))
    for lucky_id, gift in state.lucky.items():
        turns.append(Turn(f"lucky:{lucky_id}", "幸運兒", "🍀", LUCKY_ABILITY[gift], 22.5,
                          game.night_seconds, [game.players[lucky_id]]))
    return turns


def next_turn(game: Game, state: NightState) -> Turn | None:
    """The next turn in priority order. Recomputed after every turn because turns can change
    who is who (thief) or add turns (lovers, lucky)."""
    waiting = [t for t in candidate_turns(game, state) if t.key not in state.done]
    return min(waiting, key=lambda t: t.priority) if waiting else None


def unsupported_roles(game: Game) -> list[str]:
    """Roles whose JSON uses something the engine cannot run yet (see game/validate.py)."""
    from .validate import role_problems  # imported here: validate itself imports this module
    return [game.data["roles"][r]["name"] for r in dict.fromkeys(game.role_list)
            if role_problems(game.data, r)]


# ---------------------------------------------------------------- choices
def resource_left(player: Player, name: str) -> int:
    res = player.state["resources"].get(name)
    if res is None:
        return 0
    return res.get("current", res["initial"])


def use_resource(player: Player, name: str) -> None:
    res = player.state["resources"][name]
    res["current"] = res.get("current", res["initial"]) - 1


def guard_options(game: Game, guard: Player) -> list[Player]:
    s = settings(game)
    last = custom(guard).get("last_guarded_player_id")
    options = alive(game)
    if not s["guard_can_self_protect"]:
        options = [p for p in options if p.id != guard.id]
    if s["guard_same_target_restriction"] and last is not None:
        options = [p for p in options if p.id != last]
    return options


def seer_options(game: Game, seer: Player) -> list[Player]:
    return [p for p in alive(game) if p.id != seer.id]


def options_for(game: Game, state: NightState, actor: Player, ability: str) -> list[Player]:
    """Who this actor may pick for a one-target ability."""
    others = [p for p in alive(game) if p.id != actor.id]
    if ability == "guard_protect":
        return guard_options(game, actor)
    if ability in ("curse_player", "silence_player"):
        key = "last_cursed_player_id" if ability == "curse_player" else "last_silenced_player_id"
        return [p for p in others if p.id != custom(actor).get(key)]
    if ability == "fear_player":
        last = custom(actor).get("last_feared_player_id")
        options = [p for p in others if p.id != last]  # not two nights in a row
        if state.number == 1 or not settings(game)["nightmare_can_fear_wolves_after_first_night"]:  # night 1: never
            options = [p for p in options if p.state["faction"] != "WEREWOLF"]
        return options
    if ability == "charm_player":
        last = custom(actor).get("last_charmed")
        return [p for p in others if p.id != last]
    if ability in ("swap_two_players",):
        return [p for p in alive(game) if p.id not in game.swapped]
    if ability in ("bind_lovers", "bind_three"):
        return alive(game)
    if ability == "lucky_protect":
        return alive(game)
    return others


def has_tag(game: Game, player: Player, tag: str) -> bool:
    role = game.role_of(player)
    return bool(role and tag in role.get("tags", []))


def living_neighbors(game: Game, player: Player) -> list[Player]:
    """The living player on each side (by seat, around the table). The bear trainer's two neighbours,
    and the two players bound with the fortune teller's target."""
    living = sorted(alive(game), key=lambda p: p.seat)
    if player not in living:  # a dead target: take the living seats around its seat
        before = [p for p in living if p.seat < player.seat] or living[-1:]
        after = [p for p in living if p.seat > player.seat] or living[:1]
        found = [before[-1], after[0]]
    else:
        i = living.index(player)
        found = [living[i - 1], living[(i + 1) % len(living)]]
    unique = {p.id: p for p in found}
    return [p for p in unique.values() if p.id != player.id]


def night_kill_possible(game: Game) -> bool:
    """Can anyone still kill at night? A living wolf, a witch with poison, a witch hunter, a dream catcher
    (two nights on the same player), or a miracle merchant who can still give a poison."""
    for p in alive(game):
        if p.state.get("faction") == "WEREWOLF":
            return True
        role = game.role_of(p)
        ids = {a["id"] for a in role["abilities"]}
        if "use_poison" in ids and resource_left(p, "poison") > 0:
            return True
        if ids & {"hunt_suspect", "assign_sleepwalker"}:
            return True
        if "grant_lucky_skill" in ids and resource_left(p, "miracle_gift") > 0:
            return True
    return False


def bear_neighbors(game: Game, bear: Player, swap: dict[int, int]) -> list[Player]:
    """The two living neighbours the bear really sits next to: living players by seat, with the
    magician's swapped pair exchanging places."""
    ring = sorted(alive(game), key=lambda p: p.seat)
    ids = [p.id for p in ring]
    for a, b in swap.items():
        if a < b and a in ids and b in ids:  # each pair once
            i, j = ids.index(a), ids.index(b)
            ring[i], ring[j] = ring[j], ring[i]
            ids[i], ids[j] = ids[j], ids[i]
    if bear.id not in ids:
        return []
    i = ids.index(bear.id)
    near = {ring[i - 1].id: ring[i - 1], ring[(i + 1) % len(ring)].id: ring[(i + 1) % len(ring)]}
    return [p for p in near.values() if p.id != bear.id]


def wolf_options(game: Game, state: NightState) -> list[Player]:
    """Who the wolf pack may attack: everyone alive, except that the fortune teller's binding limits
    them to the three bound players, and a wolf beauty cannot be knifed."""
    options = alive(game)
    bound = state.extra.get("bound")
    if bound:
        options = [p for p in options if p.id in bound]
    return [p for p in options if not has_tag(game, p, "no_knife")]


def seer_result(target: Player) -> str:
    return "狼人" if target.state.get("seer_result") == "WEREWOLF" else "好人"


def witch_options(game: Game, witch: Player, state: NightState) -> dict:
    """What the witch may do tonight."""
    s = settings(game)
    victim = state.wolf_target
    healed = bool(state.acts.get("use_antidote", {}).get(witch.id))
    poisoned = bool(state.acts.get("use_poison", {}).get(witch.id))
    self_ok = s["witch_first_night_self_heal"] if state.number == 1 else s["witch_later_nights_self_heal"]
    can_heal = (
        resource_left(witch, "antidote") > 0
        and victim is not None
        and (victim != witch.id or self_ok)
        and not poisoned
    )
    can_poison = resource_left(witch, "poison") > 0 and not healed
    return {"victim": victim, "can_heal": can_heal, "can_poison": can_poison, "self_heal_ok": bool(self_ok)}


def thief_options(game: Game) -> list[int]:
    """Indexes of the bottom cards the thief may take. A wolf card must be taken if there is one."""
    wolves = [i for i, rid in enumerate(game.bottom_cards)
              if game.data["roles"][rid]["initial_state"]["faction"] == "WEREWOLF"]
    return wolves or list(range(len(game.bottom_cards)))


# ---------------------------------------------------------------- wolf vote
def tally_wolf_votes(votes: dict[int, int | None], valid_ids: list[int]) -> int | None:
    """Most-voted option wins; abstaining (None) is an option too. A tie is broken
    randomly among the tied options. If nobody votes, or abstain wins, nobody is attacked."""
    counts: dict[int | None, int] = {}
    for target in votes.values():
        if target is None or target in valid_ids:
            counts[target] = counts.get(target, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    return random.choice([t for t, c in counts.items() if c == best])


# ---------------------------------------------------------------- what a finished turn changes
def finish_turn(game: Game, state: NightState, turn: Turn) -> None:
    """Apply the permanent effects of a turn right away, because later turns depend on them."""
    state.done.add(turn.key)
    ability = turn.ability_id
    actors = [a for a in turn.actors if a.state["is_alive"] and a.id not in state.feared]  # feared: no skill

    if turn.key == "wolves":
        if wolves_blocked(game, state):
            state.wolf_target = None
            state.log.append("狼隊有人被夢魘恐懼，今晚無法襲擊。")
        else:
            state.wolf_target = state.redirect(tally_wolf_votes(state.wolf_votes, [p.id for p in wolf_options(game, state)]))
        return

    if ability == "assign_sleepwalker":  # mandatory: a dream catcher who did not choose gets a random target
        for actor in actors:
            others = [p.id for p in alive(game) if p.id != actor.id]
            if actor.id not in state.acts.get(ability, {}) and others:
                state.record(ability, actor.id, random.choice(others))

    for actor in actors:
        if ability in ("curse_player", "silence_player"):
            key = "last_cursed_player_id" if ability == "curse_player" else "last_silenced_player_id"
            custom(actor)[key] = state.acts.get(ability, {}).get(actor.id)
            continue

        if ability == "fear_player":
            target = state.acts.get(ability, {}).get(actor.id)
            custom(actor)["last_feared_player_id"] = target
            if target is not None:
                state.feared.add(target)
                state.log.append(f"夢魘恐懼了 {label(game, target)}。")

        elif ability == "pick_bottom_card_role":
            allowed = thief_options(game)
            if not allowed:  # no bottom cards to take
                continue
            pick = state.extra.get("thief_pick", {}).get(actor.id)
            if pick not in allowed:
                pick = random.choice(allowed)
            new_role = game.bottom_cards[pick]
            game.give_role(actor, new_role)
            state.log.append(f"盜賊 {label(game, actor.id)} 換成了「{game.data['roles'][new_role]['name']}」。")

        elif ability == "bind_three":
            target = state.acts.get(ability, {}).get(actor.id)
            if target is not None and resource_left(actor, "bind_charge") > 0:
                use_resource(actor, "bind_charge")
                bound = [target] + [p.id for p in living_neighbors(game, game.players[target])]
                state.extra["bound"] = list(dict.fromkeys(bound))
                state.log.append("占卜師綁定了 " + "、".join(label(game, i) for i in state.extra["bound"]) + "。")
        elif ability == "bind_lovers":
            pair = state.extra.get("lovers", {}).get(actor.id)
            if not pair or len(set(pair)) != 2:
                if len(alive(game)) < 2:  # nobody to link
                    continue
                pair = [p.id for p in random.sample(alive(game), 2)]
            bind_lovers(game, actor, pair)
            state.extra["lovers_pending"] = True
            state.log.append(f"邱比特連結了情侶：{label(game, pair[0])}、{label(game, pair[1])}。")

        elif ability == "choose_role_model":
            target = state.acts.get(ability, {}).get(actor.id)
            others = [p.id for p in alive(game) if p.id != actor.id]
            if target is None and others:
                target = random.choice(others)
            if target is not None:
                custom(actor)["role_model_id"] = target

        elif ability == "swap_two_players":
            pair = state.extra.get("swap", {}).get(actor.id)
            if pair and len(set(pair)) == 2:
                state.swap = {pair[0]: pair[1], pair[1]: pair[0]}
                if state.wolf_target is not None:  # the wolves already chose: the swap sends their attack elsewhere
                    state.wolf_target = state.redirect(state.wolf_target)
                game.swapped |= set(pair)
                state.log.append(f"魔術師交換了 {label(game, pair[0])} 與 {label(game, pair[1])}。")

        elif ability == "mimic_target_ability":
            target = state.acts.get(ability, {}).get(actor.id)
            if target is not None:
                copy_abilities(game, actor, game.players[target])

        elif ability == "grant_lucky_skill":
            target = state.acts.get(ability, {}).get(actor.id)
            gift = state.extra.get("gift", {}).get(actor.id)
            if target is not None and gift:
                use_resource(actor, "miracle_gift")
                if game.players[target].state["faction"] == "WEREWOLF":
                    state.extra.setdefault("backfire", []).append(actor.id)
                    state.log.append(f"奇蹟商人 {label(game, actor.id)} 把禮物給了狼人，將會反噬。")
                else:
                    state.lucky[target] = gift


def bind_lovers(game: Game, cupid: Player, pair: list[int]) -> None:
    game.lovers = (pair[0], pair[1])
    game.cupid_id = cupid.id
    game.links += [(pair[0], pair[1], "LOVER_LINK"), (pair[1], pair[0], "LOVER_LINK")]
    factions = {game.players[i].state["faction"] for i in pair}
    if "WEREWOLF" in factions and factions != {"WEREWOLF"}:
        game.lovers_mixed = True
        rule = next(r for r in cupid_rules(game, cupid) if r["trigger"] == "LOVERS_MIXED_FACTION")
        for pid in (*pair, cupid.id):
            for effect in rule["effects"]:
                if effect["type"] == "SET_FACTION":
                    game.players[pid].state["faction"] = effect["faction"]
                elif effect["type"] == "SET_WIN_CONDITION":
                    game.players[pid].state["win_condition"] = effect["win_condition"]


def cupid_rules(game: Game, cupid: Player) -> list[dict]:
    return game.role_of(cupid).get("dynamic_rules", [])


def copy_abilities(game: Game, wolf: Player, target: Player) -> None:
    """Mechanical wolf: learn the target's skills (usable from night 2) and seer result."""
    role = game.role_of(target)
    custom(wolf)["copied_from_player_id"] = target.id
    custom(wolf)["copied_role"] = target.role_id
    wolf.state["seer_result"] = target.state.get("seer_result", "VILLAGER")
    for name, res in copy.deepcopy(role.get("resources", {})).items():
        wolf.state["resources"].setdefault(name, res)
    for key, value in copy.deepcopy(role.get("custom_data", {})).items():
        custom(wolf).setdefault(key, value)


# ---------------------------------------------------------------- resolution
def _immune(role: dict, cause: str) -> bool:
    """Does the role's passive ability make it immune to this cause at night? (from the JSON)"""
    for ab in role["abilities"]:
        for effect in ab["effects"]:
            if effect["type"] == "IMMUNE_NIGHT_DEATH":
                return True
            if effect["type"] == "IMMUNE_DEATH_CAUSE" and cause in effect.get("causes", []):
                return True
    return False


def _immune_charm(role: dict) -> bool:
    """The old rogue cannot be charmed by the wolf beauty."""
    return any(e["type"] == "IMMUNE_CHARM" for ab in role["abilities"] for e in ab["effects"])


def _reflects(role: dict, trigger: str) -> bool:
    return any(ab["trigger"] == trigger for ab in role["abilities"])


def resolve_night(game: Game, state: NightState) -> NightResult:
    """The night_pipeline stages 2-6: protections, damage, heal and immunity, commit deaths, chains."""
    s = settings(game)
    log = state.log
    faction = lambda pid: game.players[pid].state["faction"]  # noqa: E731

    # 2. protections and sleepwalkers
    guarded = set(state.targets("guard_protect")) | set(state.targets("lucky_protect"))
    sleepwalkers: dict[int, int] = {}
    dream_kills: list[tuple[int, str]] = []
    for dc, target in state.acts.get("assign_sleepwalker", {}).items():
        last = custom(game.players[dc]).get("last_dream_target")
        custom(game.players[dc])["last_dream_target"] = target
        if target is None:
            continue
        if target == last:
            dream_kills.append((target, "DREAM_CATCHER_EFFECT"))  # same player two nights in a row
        else:
            sleepwalkers[target] = dc

    # 3. damage events: (target, cause)
    damage: list[tuple[int, str]] = []
    if state.wolf_target is not None:
        damage.append((state.wolf_target, "WOLF_ATTACK"))
    for ability in ("use_poison", "lucky_poison"):
        damage += [(t, "WITCH_POISON") for t in state.targets(ability)]
    for hunter_id, target in state.acts.get("hunt_suspect", {}).items():
        if target is None:
            continue
        if faction(target) == "WEREWOLF":
            damage.append((target, "WITCH_HUNTER_HUNT"))
        else:
            damage.append((hunter_id, "WITCH_HUNTER_HUNT"))  # wrong guess: the hunter pays
    # 4. immunity, then guard / antidote
    healed = set(state.targets("use_antidote"))
    final: list[tuple[int, str]] = []
    for target, cause in damage:
        player = game.players[target]
        if _immune(game.role_of(player), cause):
            log.append(f"{label(game, target)} 免疫了{CAUSE_NAMES.get(cause, cause)}。")
            continue
        if target in sleepwalkers and not (cause == "WITCH_HUNTER_HUNT" and target in state.acts.get("hunt_suspect", {})):
            log.append(f"{label(game, target)} 是夢遊者，免疫了夜間傷害。")
            continue
        if cause == "WOLF_ATTACK":
            if target in guarded and target in healed:
                prevented = not s["guard_and_witch_heal_conflict"]  # 同守同救
                if not prevented:
                    log.append(f"{label(game, target)} 同守同救，仍然死亡。")
            else:
                prevented = target in guarded or target in healed
            if prevented:
                log.append(f"{label(game, target)} 被狼人襲擊，但被救下。")
                continue
        final.append((target, cause))
    final += dream_kills

    # merchant backfire
    final += [(mm, "MIRACLE_MERCHANT_BACKFIRE") for mm in state.extra.get("backfire", [])]

    # reflect (nightmare wolf): the earlier skill by priority is reflected, and the charge is spent
    checks = [(40, "ON_BEING_WITCH_POISONED", a, t) for ab in ("use_poison",) for a, t in state.acts.get(ab, {}).items()]
    checks += [(22.5, "ON_BEING_WITCH_POISONED", a, t) for a, t in state.acts.get("lucky_poison", {}).items()]
    checks += [(50, "ON_BEING_SEER_CHECKED", a, t) for a, t in state.acts.get("check_faction", {}).items()]
    checks += [(22.5, "ON_BEING_SEER_CHECKED", a, t) for a, t in state.acts.get("lucky_check", {}).items()]
    for _, trigger, actor_id, target in sorted(checks, key=lambda c: c[0]):
        if target is None:
            continue
        victim = game.players[target]
        if _reflects(game.role_of(victim), trigger) and resource_left(victim, "reflect_charge") > 0:
            use_resource(victim, "reflect_charge")
            final.append((actor_id, "NIGHTMARE_REFLECT"))
            log.append(f"惡靈騎士 {label(game, target)} 反傷了 {label(game, actor_id)}。")

    # wolf beauty's charm: when she dies, the charmed die with her
    for beauty, target in state.acts.get("charm_player", {}).items():
        custom(game.players[beauty])["last_charmed"] = target
        if target is not None and not _immune_charm(game.role_of(game.players[target])):
            game.links.append((beauty, target, "WOLF_BEAUTY_LINK"))

    # guard memory for the no-consecutive rule
    for guard_id, target in state.acts.get("guard_protect", {}).items():
        custom(game.players[guard_id])["last_guarded_player_id"] = target

    # 5-6. commit deaths and chains (lovers, charm, and the dream catcher's sleepwalker)
    dream_links = [(dc, sw, "DREAM_CATCHER_EFFECT") for sw, dc in sleepwalkers.items()]
    deaths = apply_deaths(game, final, dream_links)
    living = {p.id for p in alive(game)}
    # the crow's curse fails if the target died tonight; the silenced player may still leave last words
    game.cursed = {t for t in state.targets("curse_player") if t in living}
    game.silenced = {t for t in state.targets("silence_player") if t in living}
    for pid, causes in deaths:
        log.append(f"{label(game, pid)} 死亡（{'、'.join(CAUSE_NAMES.get(c, c) for c in causes)}）。")
    return NightResult(deaths=deaths, log=list(log))
