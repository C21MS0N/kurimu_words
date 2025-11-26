# Telegram Word Game Bot

## Overview
An advanced Telegram bot featuring an interactive word game with comprehensive player statistics, multiple difficulty modes, and engaging gameplay mechanics. Players compete by submitting words matching specific criteria while building streaks and earning points.

## Recent Changes
**November 26, 2025 - FINAL SESSION**:
1. **Practice Mode (Me vs Me)** - Single-player solo challenge with `/practice [easy/medium/hard]`
   - No leaderboard updates, no scoring system
   - Pure skill-building practice sessions
   - Time elimination works but no penalties in practice
   
2. **Time Elimination Fixed**
   - Clear message: "TIME'S UP! You were eliminated due to timeout!"
   - NO point deduction (just forfeit - points before timeout still count)
   - Applied to both multiplayer and practice modes
   
3. **Database Profile Names** - Now stores Telegram profile names (first_name) instead of usernames
   - All player displays use their Telegram first_name
   - Database leaderboard shows profile names
   - Consistent across all commands and messages

4. **UptimeRobot Integration** - Bot stays online 24/7 without browser
   - Health endpoint running on port 5000
   - Automatic monitoring keeps workflow active
   - Stale message handling on restart

## Project Architecture
- **main.py**: Main bot application with game logic, database management, and Telegram handlers
- **flask_app.py**: Flask health endpoint for UptimeRobot monitoring
- **run.py**: Launcher script for both Flask and bot as separate processes
- **pyproject.toml**: Python project dependencies
- **wordgame_leaderboard.db**: SQLite database for tracking player scores and stats
- **words.txt**: 370K+ word dictionary for validation

## Game Flow
1. Players join a lobby using `/lobby` and `/join` commands
2. Game starts with `/begin` (requires 2+ players)
3. Players submit words matching:
   - Specific starting letter (random each turn)
   - Required word length (starts at 3, increases each round)
   - Must be in dictionary
   - Can't be previously used
4. Each valid word earns points equal to word length
5. Time limit: 60 seconds per turn (minus difficulty penalty)
6. Timeout = elimination (forfeit, no point deduction)

## Setup Requirements
- BOT_TOKEN environment variable (stored as secret)
- python-telegram-bot package installed
- UptimeRobot configured with health endpoint URL

## Commands

### Game Setup
- `/start` - Welcome message and command guide
- `/lobby` - Open a new game lobby
- `/join` - Join the lobby
- `/difficulty [easy/medium/hard]` - Set difficulty mode
- `/begin` - Start the game (requires 2+ players)
- `/practice [easy/medium/hard]` - Solo practice mode (Me vs Me)
- `/stop` - Stop the current game

### During Game
- `/hint` - Get 3 word suggestions (2-minute cooldown)
- `/skip_boost` - Skip your turn (3 max per game, 3-minute cooldown)
- `/rebound` - Skip & pass question to next player
- `/forfeit` - Give up turn (no point penalty, just forfeit)

### Stats & Leaderboard
- `/mystats` - View your personal statistics
- `/leaderboard [score/words/streak/longest]` - View top players by category
- `/profile [@username/user_id]` - View player profile with picture and achievements
- Reply to message + `/profile` - View that user's profile

### Achievements & Shop
- `/achievements` - View all unlocked titles
- `/settitle [title]` - Equip a title
- `/mytitle` - View current title
- `/progress` - Check title unlock progress
- `/shop` - View available boosts
- `/buy_hint /buy_skip /buy_rebound` - Purchase boosts
- `/inventory` - Check your boosts and balance

### Group Information
- `/help` - Complete gameplay guide
- `/groupdesc` - Group chat rules and description

## Game Features

### Difficulty Modes
- **Easy**: 3-10 letter words, increments every 3 rounds
- **Medium**: 3-15 letter words, increments every 2 rounds
- **Hard**: 4-20 letter words, increments every round

### Player Statistics Tracked
- Total score (sum of word lengths)
- Total words played
- Longest word and length
- Best streak achieved
- Average word length
- Games played
- Hints and skips used

### Streak System
- Build consecutive correct words
- 3+ streaks show "🔥 STREAK!" bonus
- Streaks reset on timeout, forfeit, or skip
- Best streaks saved to leaderboard

### Time System
- Turn time: 60s - (5s × difficulty_level), minimum 20s
- Timeout = elimination with forfeit (no points lost)
- Points earned before timeout still count

### Leaderboard Categories
- **Score**: Total points earned
- **Words**: Total words successfully played
- **Streak**: Best consecutive word streak
- **Longest**: Longest word ever submitted

### Achievement Titles
- **KAMI** (✨) - Exclusive to bot owner
- **LEGEND** (👑) - 1000+ total points
- **WARRIOR** (⚔️) - 10+ word streak
- **SAGE** (🧙) - 50+ words played
- **PHOENIX** (🔥) - 10+ games completed
- **SHADOW** (🌑) - 12+ letter word found

### Shop & Boosts
- **Hint** (80 pts) - 3 word suggestions, 2-min cooldown
- **Skip** (150 pts) - Skip turn, 3-max per game, 3-min cooldown
- **Rebound** (250 pts) - Skip & pass to next player

## Practice Mode
- Solo challenge against yourself
- All difficulty modes available
- Time limits apply (elimination on timeout)
- No leaderboard impact
- No scoring system
- Perfect for skill building
- Use `/practice [easy/medium/hard]` to start

## 24/7 Uptime
- Uses UptimeRobot to keep bot alive when browser closed
- Health endpoint on `https://<workspace>.repl.co/`
- Automatic stale message clearing on restart
- Never goes offline as long as UptimeRobot monitors

