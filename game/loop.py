"""The game loop: night, day, night, day... until someone wins."""
from __future__ import annotations

import asyncio
import traceback

from .day_flow import dusk, run_day
from .endgame import finish_game
from .models import Game
from .night_flow import run_night, say
from .room import teardown_room


def start_game(game: Game) -> None:
    if game.task and not game.task.done():  # a double click must not start a second loop
        return
    game.task = asyncio.create_task(_safe_game(game))


async def _safe_game(game: Game) -> None:
    try:
        while True:
            result = await run_night(game)
            outcome = await run_day(game, result)
            outcome = outcome or await dusk(game)  # night falls: the old rogue's wound kills them
            if outcome:
                await finish_game(game, outcome)
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        traceback.print_exc()
        await say(game, f"⚠️ 遊戲流程發生錯誤：{exc}\n已結束這局並還原暱稱、頻道與語音權限。")
        try:
            await teardown_room(game)  # do not leave nicknames, the wolf channel or mutes behind
        finally:
            if game.on_end:
                game.on_end()
