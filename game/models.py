from __future__ import annotations

import copy
from dataclasses import dataclass, field

import discord

from . import config

DEFAULT_PRESET = "6p_basic"

FACTION_NAMES = {
    "VILLAGER": "好人陣營",
    "WEREWOLF": "狼人陣營",
    "THIRD_PARTY": "第三方陣營",
}


class FakeMember:
    """A bot-controlled player for test mode. Acts automatically at night."""

    is_fake = True
    nick = None

    def __init__(self, uid: int, name: str):
        self.id = uid
        self.display_name = self.name = self.mention = name


@dataclass
class Player:
    member: discord.abc.User
    role_id: str | None = None
    state: dict = field(default_factory=dict)
    seat: int | None = None

    @property
    def is_fake(self) -> bool:
        return getattr(self.member, "is_fake", False)

    @property
    def id(self) -> int:
        return self.member.id


class Game:
    def __init__(
        self,
        host: discord.abc.User,
        channel_id: int,
        game_data: dict,
    ):
        self.host = host
        self.channel_id = channel_id
        self.data = game_data
        self.players: dict[int, Player] = {host.id: Player(host)}
        self.is_started = False
        self.config_mode = "preset"  # "preset" or "custom"
        self.victory_mode: str = game_data["victory_modes"]["default"]
        self.seat_nicks = True  # prefix nicknames with "[seat] "
        self.sheriff_enabled = False  # hold a 警長競選 after the first night (off by default; the host switches it on)
        self.sheriff: int | None = None  # player id of the sheriff (None: not elected, torn, or no election)
        self.badge_torn = False  # the badge is gone for good (torn up, or swallowed by self-destructing wolves)
        self.election_booms = 0  # wolves that self-destructed during the 警長競選
        self.election_retry = False  # a single boom under 雙爆: hold the election again next day
        self.night_seconds = config.SOLO_ACTION_SECONDS  # time for each night role
        self.wolf_seconds = config.WOLF_ACTION_SECONDS  # time for the wolf pack's attack only
        self.speak_seconds = config.SPEAKER_SECONDS  # time for each day speaker

        # Discord context, set by the /werewolf command
        self.guild: discord.Guild | None = None
        self.channel: discord.abc.Messageable | None = None
        self.voice_channel: discord.VoiceChannel | discord.StageChannel | None = None

        # Seats and room state (filled when roles are dealt / the room is built)
        self.seat_to_user: dict[int, int] = {}
        self.user_to_seat: dict[int, int] = {}
        self.original_nicks: dict[int, str | None] = {}
        self.changed_nicks: set[int] = set()
        self.voice_failed_warned = False
        self.voice_failed = False  # Discord refused a server mute (the bot lacks Mute Members)
        self.voice_applied: dict[int, bool] = {}  # player id -> server-muted, as last sent to Discord
        self.voice_phase: str | None = None  # "night" or "day": decides who may talk when someone rejoins
        self.dead_may_speak: set[int] = set()  # dead players giving their last words right now
        self.current_speaker: int | None = None  # the only player allowed to talk in the day
        self.private_channels: dict[int, discord.VoiceChannel] = {}  # player id -> their own channel
        self.last_card: dict[int, str] = {}  # the card text each player last received
        self.dead_zone: discord.TextChannel | None = None  # text channel only the dead can see
        self.dead_zone_members: set[int] = set()  # dead players already let into it
        self.wolf_voice: discord.VoiceChannel | None = None  # private voice channel for the wolf turn
        self.wolf_leaks: list[str] = []  # non-wolf players who can still see the wolf channel
        self.wolf_access: set[int] = set()  # players given access to the wolf channel after setup
        self.wolf_origin: dict[int, int] = {}  # wolf id -> voice channel id they were moved from
        self.room_ready = False
        self.is_over = False
        self.cursed: set[int] = set()  # the crow's targets: extra votes in today's first exile vote
        self.silenced: set[int] = set()  # the silencer's targets: cannot speak today
        self.jester_winners: list[int] = []  # jesters who were exiled (a personal win)
        self.bottom_cards: list[str] = []  # the thief's two extra cards
        self.links: list[tuple[int, int, str]] = []  # (if this player dies, that player dies, cause)
        self.lovers: tuple[int, int] | None = None
        self.cupid_id: int | None = None
        self.lovers_mixed = False  # lovers are one wolf, one good: third-party win
        self.swapped: set[int] = set()  # players the magician already swapped
        self.last_exiled: int | None = None  # for the gravekeeper
        self.delayed_deaths: list[tuple[int, str]] = []  # blood moon apostle: (player, cause)
        self.dusk_deaths: list[tuple[int, str]] = []  # old rogue: dies when night falls (player, cause)
        self.last_wolf_night = False  # a blood moon apostle just died after surviving to the last night
        self.consecutive_ties = 0  # days in a row that ended in a tie with nobody exiled
        self.bear_gone: set[int] = set()  # bear trainers whose death was announced
        self.election_hidden: set[int] = set()  # the night's dead, not announced yet: they take part in the election
        self.bomber_win = False  # the bomber blew up everyone who was still alive
        self.last_vote_record: dict[int, int | None] = {}  # who voted for whom in the latest exile vote
        self.feared_last_night: set[int] = set()  # the nightmare's targets: a feared hunter cannot shoot at dawn
        self.sealed_tonight = False  # blood moon apostle sealed the gods for the coming night
        self.lobby_message = None  # the lobby message holding the card button
        self.on_end = None  # callback that frees the channel for a new game

        # Night / day progress
        self.night_number = 0
        self.night = None  # current NightState
        self.history: list[str] = []  # key events for the final summary
        self.task = None  # asyncio task running the game loop

        self.preset_id = DEFAULT_PRESET
        self.rule_overrides: dict = {}  # rule_set settings changed by the chosen board
        self.role_list: list[str] = list(game_data["presets"][DEFAULT_PRESET]["roles"])

    def desired_mute(self, player_id: int) -> bool | None:
        """Should this player be server-muted right now? None = not our business (before/after the game).
        Night: everyone sits alone in their own channel, so nobody is muted.
        Day: everyone is muted except the current speaker, and the dead never speak."""
        if self.voice_phase == "night":
            return False
        if self.voice_phase == "day":
            player = self.players.get(player_id)
            may_speak = bool(player and (player.state.get("is_alive") or player_id in self.dead_may_speak))
            return not (may_speak and player_id == self.current_speaker)
        return None

    @property
    def speak_text(self) -> str:
        return f"{self.speak_seconds} 秒" if self.speak_seconds else "不限時"


    @property
    def fill_role(self) -> str:
        return self.data["presets"][self.preset_id]["fill_role"]

    def set_preset(self, preset_id: str) -> None:
        self.preset_id = preset_id
        preset = self.data["presets"][preset_id]
        self.role_list = list(preset["roles"])
        self.rule_overrides = dict(preset.get("rules", {}))  # this board's own rules (e.g. the witch cannot self-heal)

    def board_notes(self) -> list[str]:
        """What is special about the chosen board, for the setup screens (preset mode only)."""
        if self.config_mode != "preset":
            return []
        preset = self.data["presets"][self.preset_id]
        return [preset["note"]] if preset.get("note") else []

    def swallow_booms(self) -> int:
        """Wolves that must self-destruct in the election to swallow the badge: the host's choice, else
        单爆 for 10 players or fewer and 雙爆 from 11."""
        chosen = int({**self.data["rule_set"]["settings"], **self.rule_overrides}["sheriff_swallow_booms"])
        return chosen if chosen > 0 else (1 if len(self.role_list) - self.extra_cards <= 10 else 2)

    def rules_changed(self) -> str:
        """The choices that differ from the standard rules (empty = all standard)."""
        default = self.data["rule_set"]["settings"]
        now = {**default, **self.rule_overrides}
        parts = []
        for key, (title, choices) in CHOICE_RULES.items():
            if key in self.rule_overrides and now[key] != default[key]:
                parts.append(f"{title}：{dict(choices).get(now[key], now[key])}")
        return "；".join(parts)

    @property
    def witch_rule(self) -> str:
        """The witch's self-heal rule as it will be played (the board's value, or what the host switched)."""
        s = {**self.data["rule_set"]["settings"], **self.rule_overrides}
        return witch_text(s["witch_first_night_self_heal"], s["witch_later_nights_self_heal"])

    def role_summary(self) -> str:
        """e.g. '狼人 ×2、預言家、女巫'"""
        counts: dict[str, int] = {}
        for rid in self.role_list:
            counts[rid] = counts.get(rid, 0) + 1
        parts = []
        for rid, n in counts.items():
            name = self.data["roles"][rid]["name"]
            parts.append(f"{name} ×{n}" if n > 1 else name)
        return "、".join(parts) or "（空）"

    # --- victory ---
    def wolf_targets(self) -> list[str]:
        return list(self.data["victory_modes"]["modes"][self.victory_mode]["wolf_targets"])

    def victory_summary(self) -> str:
        vm = self.data["victory_modes"]
        targets = "、".join(vm["wolf_targets"][t] for t in self.wolf_targets())
        return f"{vm['modes'][self.victory_mode]['name']}（{targets}）"

    # --- checks ---
    @property
    def extra_cards(self) -> int:
        """The thief is dealt two extra bottom cards."""
        return 2 if "thief" in self.role_list else 0

    def count_check(self) -> tuple[bool, str]:
        """Do the attending players and the chosen characters add up? The thief's two bottom cards are extra.
        Returns (ok, one line saying so)."""
        players, cards = len(self.players), len(self.role_list)
        need = players + self.extra_cards
        extra = f"（含盜賊的 {self.extra_cards} 張底牌）" if self.extra_cards else ""
        if cards == need:
            return True, f"✅ 玩家 {players} 人，角色 {cards} 張{extra}，數量相符。"
        return False, f"⚠ 人數與角色數量不符：玩家 {players} 人，角色 {cards} 張{extra}，需要 {need} 張才能開始。"

    def attendees_text(self) -> str:
        return "、".join(p.member.display_name for p in self.players.values()) or "（沒有人）"

    def problems(self) -> list[str]:
        """Reasons the game cannot start yet (empty list = ready)."""
        found = []
        ok, text = self.count_check()
        if not ok:
            found.append(text.removeprefix("⚠ "))
        if not any(self.data["roles"][r]["initial_state"]["faction"] == "WEREWOLF" for r in self.role_list):
            found.append("角色中沒有任何狼人陣營的角色，遊戲會在第一天直接結束。")
        return found

    def undeal(self) -> None:
        self.is_started = False
        for p in self.players.values():
            p.role_id, p.state, p.seat = None, {}, None
        self.seat_to_user.clear()
        self.user_to_seat.clear()
        self.bottom_cards.clear()

    def add_fake_player(self) -> Player:
        n = sum(1 for p in self.players.values() if p.is_fake) + 1
        fake = FakeMember(-n, f"假人{n}")
        self.players[fake.id] = Player(fake)
        return self.players[fake.id]

    def add_player(self, user: discord.abc.User) -> bool:
        if user.id in self.players:
            return False
        self.players[user.id] = Player(user)
        return True

    def role_of(self, player: Player) -> dict | None:
        return self.data["roles"].get(player.role_id)

    def give_role(self, player: Player, role_id: str) -> None:
        """Set a player's role and build their state from player_state_template."""
        role = self.data["roles"][role_id]
        init = role["initial_state"]
        state = copy.deepcopy(self.data["player_state_template"])
        state.update(
            player_id=player.id,
            role_id=role_id,
            faction=init["faction"],
            win_condition=init["win_condition"],
            seer_result=init["seer_result"],
            resources=copy.deepcopy(role.get("resources", {})),
            custom_state=copy.deepcopy(role.get("custom_data", {})),
        )
        player.role_id = role_id
        player.state = state

    def is_pack_wolf(self, player: Player) -> bool:
        """A wolf who wakes with the pack: a normal or special wolf (not a solo wolf), or a
        player who joined the pack later (wild child, thief). A lover wolf stays in the pack."""
        role = self.role_of(player)
        if role is None:
            return False
        if player.state.get("custom_state", {}).get("joined_pack"):
            return True
        init = role["initial_state"]
        return init["faction"] == "WEREWOLF" and init["category"] != "SOLO_WOLF"

    def wolf_teammates(self, player: Player) -> list[Player]:
        """Other wolves who know each other."""
        if not self.is_pack_wolf(player):
            return []
        return [p for p in self.players.values() if p.id != player.id and self.is_pack_wolf(p)]

    def player_at(self, seat: int) -> Player:
        return self.players[self.seat_to_user[seat]]

    def config_text(self) -> str:
        """This round's character configuration, e.g. '狼人 ×3、預言家、女巫'. The thief's two bottom cards are counted."""
        extra = f"（含盜賊的 {self.extra_cards} 張底牌）" if self.extra_cards else ""
        return f"{self.role_summary()}　共 {len(self.role_list)} 張{extra}"

    def sheriff_text(self) -> str:
        """Is there a 警長競選 this game, and who is the sheriff now."""
        if not self.sheriff_enabled:
            return "警長競選：關（本局沒有警長）"
        if self.sheriff is not None:
            p = self.players[self.sheriff]
            name = p.member.display_name.removeprefix(f"[{p.seat}] ")
            return f"警長競選：開｜目前警長：{p.seat} 號 {name}"
        if self.badge_torn:
            return "警長競選：開｜警徽已失效（被撕毀或被狼人自爆吞掉），目前沒有警長"
        if self.night_number == 0:
            return "警長競選：開｜目前還沒有警長（第一天天亮後競選）"
        return "警長競選：開｜目前沒有警長"

    def seat_list_text(self) -> str:
        """The seating table as plain text (no pings), e.g. for each player's private card."""
        rows = []
        for seat in sorted(self.seat_to_user):
            p = self.player_at(seat)
            name = p.member.display_name.removeprefix(f"[{seat}] ")  # the seat nickname is already in front
            rows.append(f"{seat} 號：{name}")
        return "\n".join(rows)

    def roster_text(self) -> str:
        return "\n".join(
            f"{seat} 號：{self.player_at(seat).member.mention}" for seat in sorted(self.seat_to_user)
        )


# Rule choices the host can change in the setup panel (keys of data/rules/rule_set.json). Everything else in
# rule_set.json is a fixed rule (guard, nightmare, crow +1 vote, sheriff 1.5 votes, ...), not a setting.
CHOICE_RULES = {  # key: (title, [(value, label)])
    "tie_draw_days": ("連續平票判平局", [(0, "不啟用"), (2, "2 天"), (3, "3 天"), (4, "4 天")]),
    "sheriff_swallow_booms": ("吞警徽", [(1, "單爆（1 隻狼自爆）"), (2, "雙爆（2 隻狼自爆，12 人局）")]),
}


def rules_text(overrides: dict) -> str:
    """The board rules in words (only the witch's self-heal is a board rule so far)."""
    first, later = "witch_first_night_self_heal", "witch_later_nights_self_heal"
    if first not in overrides and later not in overrides:
        return ""
    return witch_text(overrides.get(first, True), overrides.get(later, True))


def witch_text(first: bool, later: bool) -> str:
    if first and later:
        return "女巫可以自救"
    if first:
        return "女巫首夜可以自救"
    if later:
        return "女巫第二夜起可以自救"
    return "女巫全程不可自救"


def preset_blurb(preset: dict) -> str:
    """One short line for the board list: its special rule (empty if there is none)."""
    parts = []
    if preset.get("rules"):
        parts.append(rules_text(preset["rules"]))
    if preset.get("note"):
        parts.append("見說明")
    return "，".join(p for p in parts if p)[:100]


def win_text(game: Game, player: Player) -> str:
    """How this player's side wins, from their CURRENT team (it can change: wild child, lovers) and
    the game's 遊戲模式."""
    faction = player.state["faction"]
    if player.state.get("win_condition") == "BE_EXILED":
        return "第三方（小丑）：在白天的放逐投票中被放逐，就是你的個人勝利。其他死法都算失敗。"
    if player.state.get("win_condition") == "BOMB_ALL":
        return ("第三方（炸彈人）：被放逐後炸死所有投票給你的人；若一次炸死所有存活的玩家就單獨獲勝。"
                "你還活著時，好人與狼人都不算獲勝。")
    if faction == "THIRD_PARTY":
        return ("第三方陣營（情侶＋邱比特）：消滅其他所有玩家，只剩你們存活時獲勝。"
                "情侶任一方先死，就不再有第三方勝利。")
    if faction == "WEREWOLF":
        return f"狼人陣營：依本局遊戲模式「{game.victory_summary()}」，達成任何一項即獲勝。"
    return "好人陣營：消滅所有狼人（把狼人陣營的玩家全部放逐或殺死）即獲勝。"


def describe_card(game: Game, player: Player) -> str:
    """The private text a player gets in their own channel: who they are, then
    技能介紹 / 技能判定 / 獲勝條件."""
    role = game.role_of(player)
    lines = [
        f"座位：**{player.seat} 號**",
        f"你的身分是：**【{role['name']}】** ({role['en_name']})",
        f"陣營：{FACTION_NAMES.get(player.state['faction'], player.state['faction'])}",
    ]
    if game.sheriff == player.id:
        lines.append("🎖 你是警長：放逐投票算 1.5 票，白天由你決定發言順序（你最後發言）。死亡時可以移交或撕毀警徽。")
    mates = game.wolf_teammates(player)
    if mates:
        lines.append("你的狼隊友：" + "、".join(m.member.display_name for m in mates))
    custom = player.state.get("custom_state", {})
    if game.lovers and player.id in game.lovers:
        other = game.lovers[0] if game.lovers[1] == player.id else game.lovers[1]
        lines.append(f"💞 你的情侶是 {game.players[other].seat}號 {game.players[other].member.display_name}")
    if game.lovers_mixed and (player.id in game.lovers or player.id == game.cupid_id):
        lines.append("你們屬於第三方陣營：消滅其他所有人，並存活到最後。")
    if custom.get("role_model_id") is not None:
        model = game.players[custom["role_model_id"]]
        lines.append(f"🌱 你的榜樣是 {model.seat}號 {model.member.display_name}")
    if custom.get("copied_role"):
        lines.append(f"🤖 你學會了「{game.data['roles'][custom['copied_role']]['name']}」的技能，第二夜起可以使用。")
    if custom.get("joined_pack") and player.role_id != "werewolf":
        lines.append("🐺 你現在是狼人，夜晚與狼人一起行動。")

    lines += ["", "🪑 **座位表**", game.seat_list_text(), "", "🎴 **本輪角色配置**", game.config_text(), "", "🎖 **警長**", game.sheriff_text()]
    lines += ["", "📖 **技能介紹**", role["skill_description"]]
    rules = role.get("skill_rules") or []
    if rules:
        lines += ["", "⚖️ **技能判定**"] + [f"• {rule}" for rule in rules]
    lines += ["", "🏆 **獲勝條件**", win_text(game, player)]
    lines.append("\n天黑請閉眼，等待夜晚行動...")
    return "\n".join(lines)


def split_message(text: str, limit: int = 1800) -> list[str]:
    """Discord allows 2000 characters per message: split at line breaks, never in the middle of a line."""
    parts, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts
