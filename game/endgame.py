"""End of the game: public summary, then the cleanup."""
from __future__ import annotations

import discord

from .models import FACTION_NAMES, Game
from .room import teardown_room
from .win import Outcome

WINNER_TEXT = {
    "VILLAGER": ("🎉 好人陣營獲勝！", discord.Color.green()),
    "WEREWOLF": ("🐺 狼人陣營獲勝！", discord.Color.red()),
    "THIRD_PARTY": ("💞 第三方陣營（情侶與邱比特）獲勝！", discord.Color.purple()),
    "BOMBER": ("💣 炸彈人獲勝！", discord.Color.dark_orange()),
    "JESTER": ("🃏 小丑獲勝！", discord.Color.orange()),
    "DRAW": ("🤝 平局", discord.Color.light_grey()),
}


def build_summary(game: Game, outcome: Outcome) -> discord.Embed:
    title, color = WINNER_TEXT[outcome.winner]
    embed = discord.Embed(title=f"{title}（{outcome.reason}）", color=color)

    roster = []
    for seat in sorted(game.seat_to_user):
        p = game.player_at(seat)
        role = game.role_of(p)
        faction = FACTION_NAMES.get(p.state.get("faction"), "")
        status = "存活" if p.state.get("is_alive") else "死亡"
        roster.append(f"{seat}號 {p.member.display_name}：{role['name']}（{faction}）〔{status}〕")
    chunk, size, part = [], 0, 0
    for line in roster + [None]:  # one embed field holds 1024 characters, so split the roster
        if line is None or size + len(line) + 1 > 1000:
            if chunk:
                part += 1
                embed.add_field(name="🪑 座位與身分" + (f"（{part}）" if part > 1 or line else ""),
                                value="\n".join(chunk), inline=False)
            chunk, size = [], 0
        if line is not None:
            chunk.append(line)
            size += len(line) + 1

    if game.jester_winners:  # the jester's personal win, whoever else won
        names = "、".join(f"{game.players[i].seat}號 {game.players[i].member.display_name}" for i in game.jester_winners)
        embed.add_field(name="🃏 個人勝利", value=f"小丑：{names}（被放逐）", inline=False)
    log = "\n".join(f"• {line}" for line in game.history) or "（無紀錄）"
    if len(log) > 1024:
        log = "…\n" + log[-1000:]
    embed.add_field(name="📜 復盤", value=log, inline=False)
    embed.set_footer(text=f"共進行 {game.night_number} 夜")
    return embed


async def finish_game(game: Game, outcome: Outcome) -> None:
    """Post the summary (public messages stay), then restore nicknames, delete the wolf voice
    channel, restore voice permissions, and free the channel for a new /werewolf."""
    game.is_over = True
    try:
        await game.channel.send(embed=build_summary(game, outcome),
                                allowed_mentions=discord.AllowedMentions.none())
        if game.lobby_message:
            try:
                await game.lobby_message.edit(view=None)  # the card button is pointless now
            except (discord.NotFound, discord.HTTPException):
                pass
    finally:
        await teardown_room(game)
        if game.on_end:
            game.on_end()
