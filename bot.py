import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from game_data import load_game_data

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

EXTENSIONS = ["cogs.lobby"]


class WerewolfBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.game_data = load_game_data()

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        await self.tree.sync()

    async def on_ready(self):
        print(f"Bot 已上線：{self.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("找不到 DISCORD_TOKEN，請在 .env 設定。")
    WerewolfBot().run(TOKEN)
