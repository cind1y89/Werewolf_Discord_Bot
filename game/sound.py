"""The soundboard beep for the 30 / 10 second reminders. Discord's own 'airhorn' sound is sent to the voice
channel the people are in. Everything here is best effort: if the sound cannot be played (the bot lacks
Speak / Use Soundboard, the sound is not found, Discord refuses), the game simply goes on without it."""
from __future__ import annotations

import asyncio

import discord

from . import config
from .models import Game

_default_sound = None  # looked up once
_lookup_failed = False


async def _find_sound(game: Game):
    """The sound named config.SOUND_NAME: the server's own first, else one of Discord's default sounds."""
    global _default_sound, _lookup_failed
    name = config.SOUND_NAME.lower()
    try:
        for sound in game.guild.soundboard_sounds:
            if sound.name.lower() == name:
                return sound
    except AttributeError:
        pass
    if _default_sound is None and not _lookup_failed and game.client is not None:
        try:
            for sound in await game.client.fetch_soundboard_default_sounds():
                if sound.name.lower() == name:
                    _default_sound = sound
                    break
            else:
                _lookup_failed = True
        except (discord.HTTPException, AttributeError):
            _lookup_failed = True
    return _default_sound


async def beep(game: Game, channel: discord.abc.GuildChannel | None) -> None:
    """Play the reminder sound in this voice channel. The bot moves there first (it can only be in one voice
    channel at a time), so at night it follows whoever is acting. Never raises."""
    if not config.SOUND_ENABLED or channel is None or game.guild is None:
        return
    try:
        sound = await _find_sound(game)
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
