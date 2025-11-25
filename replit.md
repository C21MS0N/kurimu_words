# Telegram Word Game Bot

## Overview
This is a Telegram bot that runs an interactive word game where players take turns submitting words that meet specific criteria. The game increases in difficulty as players progress through rounds.

## Recent Changes
**November 25, 2025**: Fixed critical bug - Changed dependency from `telegram` to `python-telegram-bot` in pyproject.toml. The incorrect `telegram` package was causing import failures. Fixed by:
1. Correcting pyproject.toml to only include `python-telegram-bot>=21.0`
2. Removing conflicting namespace package remnants from site-packages
3. Running `uv sync --reinstall` to cleanly reinstall dependencies
4. Added database file to .gitignore for cleaner version control

The bot now runs successfully and connects to Telegram API.

## Project Architecture
- **main.py**: Main bot application with game logic, database management, and Telegram handlers
- **pyproject.toml**: Python project dependencies
- **wordgame_leaderboard.db**: SQLite database for tracking player scores (auto-created)
- **words.txt**: Optional dictionary file for word validation (uses fallback if missing)

## Game Flow
1. Players join a lobby using `/lobby` and `/join` commands
2. Game starts with `/begin` (requires 2+ players)
3. Players submit words matching:
   - Specific starting letter (random each turn)
   - Required word length (starts at 3, increases each round)
   - Must be in dictionary
   - Can't be previously used
4. Each valid word earns points tracked in the leaderboard

## Setup Requirements
- BOT_TOKEN environment variable (stored as secret)
- python-telegram-bot package installed

## Commands
- `/start` - Welcome message and command list
- `/lobby` - Open a new game lobby
- `/join` - Join the lobby
- `/begin` - Start the game
- `/stop` - Stop the current game
- `/leaderboard` - View top players
