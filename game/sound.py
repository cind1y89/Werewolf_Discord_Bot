"""The soundboard beep for the 30 / 10 second reminders. Discord's own 'airhorn' sound is sent to the voice
channel the people are in. Everything here is best effort: if the sound cannot be played (the bot lacks
Speak / Use Soundboard, the sound is not found, Discord refuses), the game simply goes on without it."""
from __future__ import annotations

import asyncio

import discord

from . import config
from .models import Game

_defaults: list | None = None  # Discord's default sounds, fetched once


def _key(name: str) -> str:
    """'Ba Dum Tss', 'ba_dum_tss' and 'badumtss' are the same sound."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


async def _find_sound(game: Game, name: str):
    """The sound with this name: the server's own first, else one of Discord's default sounds."""
    global _defaults
    want = _key(name)
    try:
        for sound in game.guild.soundboard_sounds:
            if _key(sound.name) == want:
                return sound
    except AttributeError:
        pass
    if _defaults is None and game.client is not None:
        try:
            _defaults = list(await game.client.fetch_soundboard_default_sounds())
        except (discord.HTTPException, AttributeError):
            return None
    return next((s for s in _defaults or [] if _key(s.name) == want), None)


async def beep(game: Game, channel: discord.abc.GuildChannel | None, left: int | None = None) -> None:
    """Play the reminder sound for `left` seconds remaining (config.SOUND_NAMES) in this voice channel. The bot moves there first (it can only be in one voice
    channel at a time), so at night it follows whoever is acting. Never raises."""
    if not config.SOUND_ENABLED or channel is None or game.guild is None:
        return
    try:
        name = config.SOUND_NAMES.get(left) or next(iter(config.SOUND_NAMES.values()), None)
        sound = await _find_sound(game, name) if name else None
        if sound is None:
            return
        me = game.guild.me
        if not me.voice or me.voice.channel is None or me.voice.channel.id != channel.id:
            await game.guild.change_voice_state(channel=channel, self_mute=False, self_deaf=False)
            await asyncio.sleep(config.SOUND_JOIN_WAIT)  # Discord needs a moment to register the move
        await channel.send_sound(sound)
    except Exception:  # noqa: BLE001  a missing sound must never hurt the game
        return


async def leave(game: Game) -> None:
    """The game is over: take the bot out of the voice channel."""
    try:
        if game.guild is not None and game.guild.me.voice and game.guild.me.voice.channel is not None:
            await game.guild.change_voice_state(channel=None)
    except Exception:  # noqa: BLE001
        pass
