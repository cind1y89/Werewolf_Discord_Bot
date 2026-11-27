# Werewolf Bot

A Discord bot that hosts a game of Werewolf (狼人殺) in a text with voice channel. It deals roles, runs the night and day phases, handles the sheriff election, votes and win conditions. The bot's messages are in Traditional Chinese.

## Requirements

- Python 3.10 or newer
- A Discord application with a bot token (https://discord.com/developers/applications)

## Setup

1. Create a bot in the Discord Developer Portal.
   - Under **Bot**, enable the **Server Members Intent**.
   - Copy the bot token.
   - Under **OAuth2 > URL Generator**, select the scopes `bot` and `applications.commands`, then invite the bot to your server.
   - The bot creates a private channel for each player (`seat-N`) to deliver their role, so it needs permission to manage channels and roles.

2. Find `.env` file in the project root:

   ```
   DISCORD_TOKEN=your-bot-token-here
   ```

## Discord Setting(身份組)
- Remove admin permissions(管理者) from everyone in the Discord teams(Expect the Team Creator). For the better game experience

- For the bot, set the permission for

   一般權限 
   - 檢視頻道
   - 管理頻道
   - 管理身份組
   - 管理暱稱
   - 禁言成員

   文字頻道權限 
   - 傳送訊息和建立貼文
   - 嵌入連結
   - 讀取歷史訊息

   語音頻道權限 
   - 連接
   - 說話
   - 使用音效版
   - 使用語音活動
   - 將成員靜音
   - 將成員拒聽
   - 移動成員
   - 設定語音頻道

## Run the Code

1. Create the virtual environment and install dependencies:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```

Or without activating: `./.venv/bin/python bot.py`. The console prints `Bot 已上線：<name>` when it is connected.

If Python is upgraded (for example by Homebrew), the venv breaks. Delete `.venv` and repeat step 

## How to play

| Command | What it does |
| --- | --- |
| `/werewolf` | Opens a new game lobby in the current channel. |
| `/reset` | Force-ends and resets the game in the current channel. |

1. Run `/werewolf`. Players press **加入遊戲** (Join) to sign up.
2. Press **設定遊戲** (Settings) to pick a role preset or a custom role list, the victory mode, phase timers and rule options.
3. Press **開始發牌** (Deal), review the setup and press **確認開始** (Confirm).
4. Each player's role is posted in their own `seat-N` channel, visible only to them.
5. Press **天黑請閉眼** to start the first night.

The lobby also has test buttons: **加假人** adds a fake player, and **全部身分** shows every role. Use them only when testing.

## Project layout

```
bot.py            Entry point; loads the bot and the cogs
game_data.py      Loads the JSON game data
cogs/lobby.py     /werewolf and /reset commands and the lobby, settings and confirm UI
game/             Game engine: setup, night, day, deaths, sheriff, votes, win conditions
data/roles/       One JSON file per role (villager, werewolf, third_party)
data/abilities/   Shared ability definitions
data/presets/     Role presets (board layouts)
data/rules/       Rule options and victory modes
data/enums/       Enumerations used by the role and ability files
```

## Adding or changing roles

Roles are data files. Add a JSON file under `data/roles/<faction>/` following an existing one, for example `data/roles/villager/seer.json`. See `data/roles/roleType.md` for the role types. Roles whose abilities are not implemented yet are dealt but do not act. The confirm screen lists them.
