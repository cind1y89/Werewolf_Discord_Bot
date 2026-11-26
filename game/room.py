"""Discord-side room setup and teardown: seat nicknames, wolf channel, voice snapshot."""
from __future__ import annotations

import asyncio

import discord

from .models import Game, describe_card, split_message

NICK_LIMIT = 32


def check_permissions(game: Game) -> list[str]:
    """Missing bot permissions that block starting. Empty list = OK."""
    me = game.guild.me
    missing = []

    ch = game.channel.permissions_for(me)
    for attr, label in (
        ("view_channel", "查看頻道"), ("send_messages", "發送訊息"),
        ("embed_links", "嵌入連結"), ("read_message_history", "讀取訊息記錄"),
    ):
        if not getattr(ch, attr):
            missing.append(f"本頻道缺少「{label}」")

    if not me.guild_permissions.manage_channels:
        missing.append("缺少「管理頻道」（建立狼人頻道）")
    if game.seat_nicks and not me.guild_permissions.manage_nicknames:
        missing.append("缺少「管理暱稱」（或在設定中關閉座位暱稱）")

    if game.voice_channel:
        gp = me.guild_permissions
        for attr, label in (
            ("mute_members", "禁言成員（發言時靜音/解除靜音）"),
            ("move_members", "移動成員（夜晚各自回頻道、狼人移到狼人語音）"),
            ("manage_roles", "管理身分組（建立只有本人看得到的頻道）"),
        ):
            if not getattr(gp, attr):
                missing.append(f"伺服器缺少「{label}」")
    return missing


def find_wolf_channel_leaks(game: Game) -> list[str]:
    """Non-wolf real players who can still see the wolf channel, with the reason."""
    leaks = []
    for player in game.players.values():
        if player.is_fake or game.is_pack_wolf(player):
            continue
        member = game.guild.get_member(player.id) or player.member
        if not game.wolf_voice.permissions_for(member).view_channel:
            continue
        if game.guild.owner_id == member.id:
            reason = "伺服器擁有者"
        elif member.guild_permissions.administrator:
            reason = "擁有「管理員」權限"
        else:
            reason = "頻道權限設定有誤"
        leaks.append(f"{member.display_name}（{reason}）")
    return leaks


async def setup_room(game: Game) -> list[str]:
    """Build everything the game needs. Returns display names whose nickname could not be changed.
    Raises on a hard failure; the caller should then call teardown_room()."""
    failed_nicks: list[str] = []

    if game.seat_nicks:
        for player in game.players.values():
            if player.is_fake:
                continue
            member = player.member
            game.original_nicks[member.id] = member.nick  # None = no server nickname
            prefix = f"[{player.seat}] "
            nick = prefix + member.display_name[: NICK_LIMIT - len(prefix)]
            try:
                await member.edit(nick=nick, reason="狼人殺座位暱稱")
                game.changed_nicks.add(member.id)
            except (discord.Forbidden, discord.HTTPException):
                failed_nicks.append(member.display_name)  # never abort the game for this


    wolves = [p for p in game.players.values() if game.is_pack_wolf(p) and not p.is_fake]
    roles = game.data["roles"]
    wolves_possible = any(roles[r]["initial_state"]["faction"] == "WEREWOLF" or r in ("thief", "wild_child")
                          for r in game.role_list)  # a thief or wild child can become a wolf later
    if (wolves or wolves_possible) and game.voice_channel:
        me = game.guild.me
        wolf_perm = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, send_messages=True, read_message_history=True
        )
        overwrites = {
            game.guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
            me: discord.PermissionOverwrite(
                view_channel=True, connect=True, move_members=True, speak=True,
                send_messages=True, read_message_history=True,
            ),
            **{p.member: wolf_perm for p in wolves},
        }
        game.wolf_voice = await game.guild.create_voice_channel(
            "wolves-den", category=game.voice_channel.category, overwrites=overwrites,
            reason="狼人殺狼人語音",
        )
        game.wolf_leaks = find_wolf_channel_leaks(game)

    await create_private_channels(game)
    await create_dead_zone(game)
    game.room_ready = True
    return failed_nicks


async def teardown_room(game: Game) -> None:
    """Undo setup_room. Safe to call at any point; each step is independent."""
    for uid in list(game.changed_nicks):
        member = game.players[uid].member if uid in game.players else None
        if member:
            try:
                await member.edit(nick=game.original_nicks.get(uid), reason="狼人殺結束，還原暱稱")
            except (discord.Forbidden, discord.HTTPException):
                pass
    game.changed_nicks.clear()
    game.original_nicks.clear()

    await move_wolves(game, to_wolf_channel=False)
    game.voice_phase = None
    await gather_players(game, mute=False)  # everyone back to the main channel, unmuted, before their channels go
    await unmute_everyone(game)
    for channel in list(game.private_channels.values()):
        try:
            await channel.delete(reason="狼人殺結束")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    game.private_channels.clear()
    game.last_card.clear()
    if game.wolf_voice:
        try:
            await game.wolf_voice.delete(reason="狼人殺結束")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        game.wolf_voice = None

    if game.dead_zone:
        try:
            await game.dead_zone.delete(reason="狼人殺結束")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        game.dead_zone = None
    game.dead_zone_members.clear()
    game.voice_applied.clear()
    game.room_ready = False


async def set_voice(game: Game, allowed: set[int]) -> None:
    """Server-mute every connected player except those in `allowed` (and only the living may talk).

    This uses Discord's server mute, which takes effect at once for someone already in the
    channel. Changing channel permissions does not: a connected person keeps their old state
    until they reconnect. Only players whose state changed are sent. Someone who is not
    connected is skipped; the voice-state listener fixes them when they join."""
    game.current_speaker = next(iter(allowed)) if len(allowed) == 1 else None

    async def apply(player, want: bool) -> None:
        try:
            await player.member.edit(mute=want, reason="狼人殺發言控制")
            game.voice_applied[player.id] = want
        except discord.Forbidden:
            game.voice_failed = True  # the bot lacks Mute Members
        except discord.HTTPException:
            pass  # not connected to voice

    jobs = []
    for pid, player in game.players.items():
        if player.is_fake:
            continue
        want = not (pid in allowed and (player.state.get("is_alive", True) or pid in game.dead_may_speak))
        if game.voice_applied.get(pid) != want and _where(player.member) is not None:
            jobs.append(apply(player, want))
    await asyncio.gather(*jobs)


async def unmute_everyone(game: Game) -> None:
    """End of the game: nobody stays server-muted."""
    jobs = []
    for pid, player in game.players.items():
        if not player.is_fake and game.voice_applied.get(pid) and _where(player.member) is not None:
            jobs.append(player.member.edit(mute=False, reason="狼人殺結束"))
    await asyncio.gather(*jobs, return_exceptions=True)
    game.voice_applied.clear()


def _where(member) -> discord.abc.GuildChannel | None:
    return member.voice.channel if member.voice else None


async def _move(game: Game, player, channel, reason: str, mute: bool | None = None) -> bool:
    """Move a player to a voice channel, and set their server mute in the same request."""
    kwargs = {"voice_channel": channel, "reason": reason}
    if mute is not None:
        kwargs["mute"] = mute
    try:
        await player.member.edit(**kwargs)
    except discord.Forbidden:
        if mute is not None:
            game.voice_failed = True
        return False
    except discord.HTTPException:
        return False
    if mute is not None:
        game.voice_applied[player.id] = mute
    return True


async def create_private_channels(game: Game) -> None:
    """One channel per real player that only that player (and the bot) can see. Their character
    is posted there, so nobody has to click anything, and at night everybody sits in their own
    channel so the wolves leaving for the den gives nothing away."""
    me = game.guild.me
    category = game.voice_channel.category if game.voice_channel else getattr(game.channel, "category", None)
    for player in game.players.values():
        if player.is_fake:
            continue
        overwrites = {
            game.guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
            me: discord.PermissionOverwrite(
                view_channel=True, connect=True, move_members=True, speak=True,
                send_messages=True, read_message_history=True,
            ),
            player.member: discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, send_messages=True, read_message_history=True,
            ),
        }
        channel = await game.guild.create_voice_channel(
            f"seat-{player.seat}", category=category, overwrites=overwrites, reason="狼人殺玩家專屬頻道"
        )
        game.private_channels[player.id] = channel
        text = describe_card(game, player)
        game.last_card[player.id] = text
        first, *rest = split_message(f"{player.member.mention} 這是你的專屬頻道，只有你看得到。\n\n{text}")
        await channel.send(first)
        for part in rest:
            await channel.send(part)


async def create_dead_zone(game: Game) -> None:
    """A text channel in the same category that only the dead (and the bot) can see: nobody is in it at the
    start, and a player is let in when they die. The dead keep hearing the game in the main voice channel
    (they are only muted), and here they can talk to each other without the living reading it."""
    if not game.guild or all(p.is_fake for p in game.players.values()):
        return
    me = game.guild.me
    category = game.voice_channel.category if game.voice_channel else getattr(game.channel, "category", None)
    overwrites = {
        game.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    try:
        game.dead_zone = await game.guild.create_text_channel(
            "dead-zone", category=category, overwrites=overwrites, reason="狼人殺死人區",
            topic="出局的玩家在這裡聊天，只有死人看得到。")
        await game.dead_zone.send("💀 **死人區**：出局的玩家會被加進來，只有死人看得到這裡的文字。"
                                  "你們仍然可以在語音頻道裡聽到遊戲（但被靜音）。**請不要透露身分給活著的人。**")
    except (discord.Forbidden, discord.HTTPException):
        game.dead_zone = None  # never stop the game for this


async def sync_dead_zone(game: Game) -> None:
    """Let in every real player who has died since the last time."""
    zone = game.dead_zone
    if zone is None:
        return
    for p in game.players.values():
        if p.is_fake or p.state.get("is_alive", True) or p.id in game.dead_zone_members:
            continue
        try:
            await zone.set_permissions(
                p.member, overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True),
                reason="狼人殺：出局")
            game.dead_zone_members.add(p.id)
            await zone.send(f"💀 {p.member.mention}（{p.seat} 號）出局了，歡迎來到死人區。")
        except (discord.Forbidden, discord.HTTPException):
            game.dead_zone_members.add(p.id)  # do not retry forever


async def refresh_cards(game: Game) -> None:
    """Post a new card to a player whose information changed (new role, lover, turned wolf...).
    Also lets the newly dead into the dead zone."""
    await sync_dead_zone(game)
    for pid, channel in game.private_channels.items():
        text = describe_card(game, game.players[pid])
        if text == game.last_card.get(pid):
            continue
        game.last_card[pid] = text
        try:
            first, *rest = split_message("🔔 你的身分資訊更新了：\n\n" + text)
            await channel.send(first)
            for part in rest:
                await channel.send(part)
        except (discord.Forbidden, discord.HTTPException):
            pass


async def isolate_players(game: Game) -> None:
    """Night: everyone in the main voice channel goes to their own channel."""
    main = game.voice_channel
    if main is None:
        return
    jobs = []
    for pid, channel in game.private_channels.items():
        member = game.players[pid].member
        here = _where(member)
        if here is not None and here.id == main.id:
            jobs.append(_move(game, game.players[pid], channel, "夜晚：回到專屬頻道", mute=False))
    await asyncio.gather(*jobs)


async def gather_players(game: Game, mute: bool = True) -> None:
    """Day: everyone in their own channel (or the wolf den) goes back to the main voice channel,
    muted in the same request (only the speaker is unmuted later)."""
    main = game.voice_channel
    if main is None:
        return
    away = {c.id for c in game.private_channels.values()}
    if game.wolf_voice:
        away.add(game.wolf_voice.id)
    jobs = []
    for pid, player in game.players.items():
        if player.is_fake:
            continue
        here = _where(player.member)
        if here is not None and here.id in away:
            jobs.append(_move(game, player, main, "白天：回到主語音頻道", mute=mute))
    await asyncio.gather(*jobs)


async def ensure_wolf_access(game: Game, player) -> None:
    """Let a player who joined the wolves later (wild child, thief, last solo wolf) use the wolf channel."""
    if game.wolf_voice is None or player.is_fake or player.id in game.wolf_access:
        return
    try:
        await game.wolf_voice.set_permissions(
            player.member, view_channel=True, connect=True, speak=True,
            send_messages=True, read_message_history=True,
        )
        game.wolf_access.add(player.id)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def move_wolves(game: Game, to_wolf_channel: bool, players=None) -> None:
    """Move the living wolves from wherever they are (main or their own channel) into the den
    for their turn, or back to where they came from. Wolves who are not in voice are skipped."""
    if game.wolf_voice is None or game.voice_channel is None:
        return
    main = game.voice_channel
    if to_wolf_channel:
        for player in players or []:
            if player.is_fake or not player.state.get("is_alive"):
                continue
            await ensure_wolf_access(game, player)
            here = _where(player.member)
            own = game.private_channels.get(player.id)
            if here is not None and here.id in {main.id, own.id if own else None}:
                game.wolf_origin[player.id] = here.id
                await _move(game, player, game.wolf_voice, "狼人回合")
        return
    # Coming back: do not trust the cached voice state, it can lag right after the move there.
    # A failed move stays in wolf_origin so the next call (at dawn) tries again.
    for pid in list(game.wolf_origin):
        member = game.players[pid].member
        here = _where(member)
        own = game.private_channels.get(pid)
        known = {game.wolf_voice.id, main.id} | ({own.id} if own else set())
        if here is None or here.id not in known:
            game.wolf_origin.pop(pid, None)  # left voice, or went somewhere else on their own
            continue
        destination = own if own and game.wolf_origin[pid] == own.id else main
        if await _move(game, game.players[pid], destination, "狼人回合結束"):
            game.wolf_origin.pop(pid, None)
