# Telegram Word Game Bot

## Overview
An advanced Telegram bot featuring an interactive word game with comprehensive player statistics, multiple difficulty modes, and engaging gameplay mechanics. Players compete by submitting words matching specific criteria while building streaks and earning points.

## Recent Changes
**November 25, 2025**: 
1. **Fixed critical bug** - Changed dependency from `telegram` to `python-telegram-bot` in pyproject.toml
   - Correcting pyproject.toml to only include `python-telegram-bot>=21.0`
   - Removing conflicting namespace package remnants
   - Running `uv sync --reinstall` to cleanly reinstall dependencies

2. **Added comprehensive dictionary** - Downloaded 370,105-word English dictionary
   - Saved as words.txt for extensive word validation
   - Supports all word lengths from the repository

3. **Major Feature Enhancements**:
   - **Player Statistics System**: Tracks total words, longest word, best streak, average word length, hints/skips used
   - **Hint System**: Get 3 word suggestions with 2-minute cooldown
   - **Skip Functionality**: Skip difficult turns (3 max per game, 3-minute cooldown)
   - **Difficulty Modes**: Easy (3-10 letters), Medium (3-15 letters), Hard (4-20 letters)
   - **Streak Tracking**: Consecutive correct words with combo bonuses and visual feedback
   - **Enhanced Leaderboard**: Multiple categories (score, words played, best streak, longest word)
   - **Personal Stats**: /mystats command shows comprehensive player performance
   - **Improved UI**: Rich formatting with emojis, medals, and progress indicators

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

### Game Setup
- `/start` - Welcome message and full command list
- `/lobby` - Open a new game lobby
- `/join` - Join the lobby
- `/difficulty [easy/medium/hard]` - Set difficulty mode
- `/begin` - Start the game (requires 2+ players)
- `/stop` - Stop the current game

### During Game
- `/hint` - Get 3 word suggestions (2-minute cooldown)
- `/skip` - Skip your turn (3 max per game, 3-minute cooldown)

### Stats & Leaderboard
- `/mystats` - View your personal statistics
- `/leaderboard [score/words/streak/longest]` - View top players by category

## Game Features

### Difficulty Modes
- **Easy**: 3-10 letter words, slower progression (increments every 3 rounds)
- **Medium**: 3-15 letter words, moderate progression (increments every 2 rounds)
- **Hard**: 4-20 letter words, fast progression (increments every round)

### Player Statistics Tracked
- Total score (sum of all word lengths)
- Total words played
- Longest word and its length
- Best streak achieved
- Average word length
- Hints and skips used

### Streak System
- Build consecutive correct words
- Visual "🔥 STREAK!" bonus at 3+ consecutive words
- Streaks reset when skipping or after incorrect attempts
- Best streaks saved to leaderboard

### Leaderboard Categories
- **Score**: Total points earned (sum of word lengths)
- **Words**: Total words successfully played
- **Streak**: Best consecutive word streak
- **Longest**: Longest word ever submitted
