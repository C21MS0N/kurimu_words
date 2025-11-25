import logging
import random
import sqlite3
import os
import asyncio
import time
from typing import List, Dict, Set, Optional
from datetime import datetime, timedelta

# Imports from the library we just installed
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ==========================================
# CONFIGURATION
# ==========================================
# We will set this up in the "Secrets" tab in the next step
BOT_TOKEN = os.environ.get("BOT_TOKEN", "REPLACE_WITH_TOKEN_IF_NOT_USING_SECRETS")

# Files
DICTIONARY_FILE = "words.txt"
DB_FILE = "wordgame_leaderboard.db"

# Game Settings
TURN_TIMEOUT = 60
HINT_COOLDOWN = 120
SKIP_COOLDOWN = 180
MAX_SKIPS_PER_GAME = 3

# Difficulty settings
DIFFICULTY_MODES = {
    'easy': {'start_length': 3, 'increment_every': 3, 'max_length': 10},
    'medium': {'start_length': 3, 'increment_every': 2, 'max_length': 15},
    'hard': {'start_length': 4, 'increment_every': 1, 'max_length': 20}
}

# ==========================================
# LOGGING SETUP
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE MANAGER (Leaderboard)
# ==========================================
class DatabaseManager:
    def __init__(self, db_name):
        self.db_name = db_name
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS leaderboard (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                total_words INTEGER DEFAULT 0,
                games_played INTEGER DEFAULT 0,
                longest_word TEXT DEFAULT '',
                longest_word_length INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0,
                total_score INTEGER DEFAULT 0,
                hints_used INTEGER DEFAULT 0,
                skips_used INTEGER DEFAULT 0,
                average_word_length REAL DEFAULT 0.0
            )
        ''')
        conn.commit()
        conn.close()

    def update_word_stats(self, user_id, username, word, streak=0):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        
        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        entry = c.fetchone()
        
        if entry:
            total_words = entry[2] + 1
            longest_word = entry[4] if len(entry[4]) > len(word) else word
            longest_word_length = max(entry[5], len(word))
            best_streak = max(entry[6], streak)
            total_score = entry[7] + len(word)
            avg_word_length = ((entry[10] * entry[2]) + len(word)) / total_words
            
            c.execute('''UPDATE leaderboard SET 
                username = ?, total_words = ?, longest_word = ?, 
                longest_word_length = ?, best_streak = ?, total_score = ?,
                average_word_length = ?
                WHERE user_id=?''', 
                (username, total_words, longest_word, longest_word_length, 
                 best_streak, total_score, avg_word_length, user_id))
        else:
            c.execute('''INSERT INTO leaderboard 
                (user_id, username, total_words, longest_word, longest_word_length, 
                 best_streak, total_score, average_word_length) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', 
                (user_id, username, 1, word, len(word), streak, len(word), float(len(word))))
        
        conn.commit()
        conn.close()
    
    def ensure_player_exists(self, user_id, username):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute('''INSERT INTO leaderboard 
                (user_id, username, total_words, total_score, average_word_length) 
                VALUES (?, ?, 0, 0, 0.0)''', (user_id, username))
            conn.commit()
        conn.close()

    def update_hint_skip(self, user_id, is_hint=True):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        if is_hint:
            c.execute("UPDATE leaderboard SET hints_used = hints_used + 1 WHERE user_id=?", (user_id,))
        else:
            c.execute("UPDATE leaderboard SET skips_used = skips_used + 1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

    def get_top_players(self, category='total_score', limit=10):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        
        valid_categories = ['total_score', 'total_words', 'longest_word_length', 'best_streak']
        if category not in valid_categories:
            category = 'total_score'
        
        c.execute(f"SELECT username, {category} FROM leaderboard ORDER BY {category} DESC LIMIT ?", (limit,))
        data = c.fetchall()
        conn.close()
        return data
    
    def get_player_stats(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        data = c.fetchone()
        conn.close()
        return data

# ==========================================
# GAME LOGIC
# ==========================================
class GameState:
    def __init__(self):
        self.is_running = False
        self.is_lobby_open = False
        self.players: List[dict] = []
        self.current_player_index = 0
        self.current_word_length = 3
        self.current_start_letter = ''
        self.used_words: Set[str] = set()
        self.turn_count = 0
        self.dictionary: Set[str] = set()
        
        self.difficulty = 'medium'
        self.player_streaks: Dict[int, int] = {}
        self.player_hints: Dict[int, float] = {}
        self.player_skips: Dict[int, float] = {}
        self.skips_remaining: Dict[int, int] = {}
        self.turn_start_time: Optional[float] = None
        self.timeout_task: Optional[asyncio.Task] = None
        
        self.load_dictionary()

    def load_dictionary(self):
        if os.path.exists(DICTIONARY_FILE):
            try:
                with open(DICTIONARY_FILE, 'r', encoding='utf-8') as f:
                    # Load words into a set for fast checking
                    self.dictionary = {line.strip().lower() for line in f}
                logger.info(f"Loaded {len(self.dictionary)} words from {DICTIONARY_FILE}")
            except Exception as e:
                logger.error(f"Error loading dictionary: {e}")
                self.use_fallback_dictionary()
        else:
            logger.warning("Dictionary file not found. Using fallback list.")
            self.use_fallback_dictionary()

    def use_fallback_dictionary(self):
        # Small list for testing if no file is uploaded
        self.dictionary = {
            "cat", "dog", "bat", "rat", "hat", "mat", "sat", "pat",
            "bird", "word", "nerd", "curd", "herd", "blue", "glue",
            "apple", "board", "chair", "dance", "eagle", "fruit",
            "banana", "friend", "orange", "purple", "school",
            "elephant", "giraffe", "internet", "keyboard"
        }

    def reset(self):
        self.is_running = False
        self.is_lobby_open = False
        self.players = []
        self.current_player_index = 0
        self.difficulty = 'medium'
        difficulty_config = DIFFICULTY_MODES[self.difficulty]
        self.current_word_length = difficulty_config['start_length']
        self.used_words = set()
        self.turn_count = 0
        self.player_streaks = {}
        self.player_hints = {}
        self.player_skips = {}
        self.skips_remaining = {}
        self.turn_start_time = None
        if self.timeout_task:
            self.timeout_task.cancel()
            self.timeout_task = None

    def set_difficulty(self, difficulty: str):
        if difficulty in DIFFICULTY_MODES:
            self.difficulty = difficulty
            config = DIFFICULTY_MODES[difficulty]
            self.current_word_length = config['start_length']
            return True
        return False

    def next_turn(self):
        self.current_player_index = (self.current_player_index + 1) % len(self.players)
        self.turn_count += 1
        
        difficulty_config = DIFFICULTY_MODES[self.difficulty]
        increment_every = difficulty_config['increment_every']
        max_length = difficulty_config['max_length']
        
        if self.turn_count > 0 and self.turn_count % (len(self.players) * increment_every) == 0:
            self.current_word_length += 1
            if self.current_word_length > max_length:
                self.current_word_length = max_length

        import string
        self.current_start_letter = random.choice(string.ascii_lowercase)
        self.turn_start_time = time.time()
    
    def get_streak(self, user_id: int) -> int:
        return self.player_streaks.get(user_id, 0)
    
    def increment_streak(self, user_id: int):
        self.player_streaks[user_id] = self.player_streaks.get(user_id, 0) + 1
    
    def reset_streak(self, user_id: int):
        self.player_streaks[user_id] = 0
    
    def can_use_hint(self, user_id: int) -> bool:
        last_hint = self.player_hints.get(user_id, 0)
        return (time.time() - last_hint) >= HINT_COOLDOWN
    
    def use_hint(self, user_id: int):
        self.player_hints[user_id] = time.time()
    
    def can_skip(self, user_id: int) -> bool:
        last_skip = self.player_skips.get(user_id, 0)
        skips_left = self.skips_remaining.get(user_id, MAX_SKIPS_PER_GAME)
        return (time.time() - last_skip) >= SKIP_COOLDOWN and skips_left > 0
    
    def use_skip(self, user_id: int):
        self.player_skips[user_id] = time.time()
        self.skips_remaining[user_id] = self.skips_remaining.get(user_id, MAX_SKIPS_PER_GAME) - 1
    
    def get_hint_words(self) -> List[str]:
        matching_words = [
            w for w in self.dictionary 
            if len(w) == self.current_word_length 
            and w.startswith(self.current_start_letter)
            and w not in self.used_words
        ]
        return random.sample(matching_words, min(3, len(matching_words))) if matching_words else []
    
    def initialize_player_stats(self, user_id: int):
        if user_id not in self.player_streaks:
            self.player_streaks[user_id] = 0
        if user_id not in self.skips_remaining:
            self.skips_remaining[user_id] = MAX_SKIPS_PER_GAME

# Key: chat_id, Value: GameState
games: Dict[int, GameState] = {}
db = DatabaseManager(DB_FILE)

# ==========================================
# BOT COMMANDS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 <b>Welcome to the Infinite Word Game!</b>\n\n"
        "📋 <b>Game Commands:</b>\n"
        "/lobby - Open a new game lobby\n"
        "/join - Join the lobby\n"
        "/begin - Start the game (needs 2+ players)\n"
        "/difficulty [easy/medium/hard] - Set difficulty\n"
        "/stop - Stop the current game\n\n"
        "🎯 <b>During Game:</b>\n"
        "/hint - Get word suggestions (2min cooldown)\n"
        "/skip - Skip your turn (3min cooldown, 3 max)\n\n"
        "📊 <b>Stats & Leaderboard:</b>\n"
        "/mystats - View your personal stats\n"
        "/leaderboard [score/words/streak/longest] - Top players\n\n"
        "💡 <b>Features:</b>\n"
        "• Streak tracking & combo bonuses\n"
        "• Three difficulty modes\n"
        "• Comprehensive player statistics\n"
        "• Cooldowns on hints and skips",
        parse_mode='HTML'
    )

async def lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        games[chat_id] = GameState()
    game = games[chat_id]

    if game.is_running:
        await update.message.reply_text("⚠️ Game in progress! Finish it or type /stop.")
        return

    if game.is_lobby_open:
        await update.message.reply_text("✅ Lobby open! Type /join to enter.")
        return

    game.reset()
    game.is_lobby_open = True
    
    user = update.effective_user
    game.players.append({'id': user.id, 'name': user.first_name})

    await update.message.reply_text(
        f"📢 <b>Lobby Opened!</b>\n\n"
        f"{user.first_name} has joined.\n"
        f"Waiting for others... Type /join to play!",
        parse_mode='HTML'
    )

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games or not games[chat_id].is_lobby_open:
        await update.message.reply_text("❌ No lobby open. Type /lobby to start one.")
        return

    game = games[chat_id]
    
    if any(p['id'] == user.id for p in game.players):
        await update.message.reply_text(f"👤 {user.first_name}, you are already in.")
        return

    game.players.append({'id': user.id, 'name': user.first_name})
    game.initialize_player_stats(user.id)
    await update.message.reply_text(f"✅ <b>{user.first_name}</b> joined! (Total: {len(game.players)})", parse_mode='HTML')

async def begin_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games: return

    game = games[chat_id]

    if not game.is_lobby_open:
        await update.message.reply_text("❌ No lobby active.")
        return

    if len(game.players) < 2:
        await update.message.reply_text("⚠️ You need at least 2 players!")
        return

    for player in game.players:
        game.initialize_player_stats(player['id'])

    game.is_lobby_open = False
    game.is_running = True
    
    import string
    game.current_start_letter = random.choice(string.ascii_lowercase)
    game.turn_start_time = time.time()
    current_player = game.players[game.current_player_index]

    difficulty_emoji = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
    await update.message.reply_text(
        f"🎮 <b>Game Started!</b>\n"
        f"Difficulty: {difficulty_emoji.get(game.difficulty, '🟡')} <b>{game.difficulty.upper()}</b>\n"
        f"Players: {', '.join([p['name'] for p in game.players])}\n\n"
        f"👉 <b>{current_player['name']}</b>'s turn!\n"
        f"Write a <b>{game.current_word_length}-letter</b> word starting with <b>'{game.current_start_letter.upper()}'</b>",
        parse_mode='HTML'
    )

async def stop_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in games:
        games[chat_id].reset()
        await update.message.reply_text("🛑 Game stopped.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category_map = {
        'score': 'total_score',
        'words': 'total_words',
        'streak': 'best_streak',
        'longest': 'longest_word_length'
    }
    
    category_input = context.args[0].lower() if context.args else 'score'
    category = category_map.get(category_input, 'total_score')
    
    top = db.get_top_players(category=category, limit=10)
    if not top:
        await update.message.reply_text("🏆 Leaderboard is empty!")
        return
    
    category_names = {
        'total_score': 'Total Score',
        'total_words': 'Words Played',
        'best_streak': 'Best Streak',
        'longest_word_length': 'Longest Word'
    }
    
    text = f"🏆 <b>Leaderboard - {category_names.get(category, 'Total Score')}</b> 🏆\n\n"
    for idx, (name, value) in enumerate(top, 1):
        emoji = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
        text += f"{emoji} <b>{name}</b> - {value}\n"
    
    text += "\n💡 Use: /leaderboard [score/words/streak/longest]"
    await update.message.reply_text(text, parse_mode='HTML')

async def difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        games[chat_id] = GameState()
    
    game = games[chat_id]
    
    if game.is_running:
        await update.message.reply_text("❌ Cannot change difficulty during an active game!")
        return
    
    if not context.args:
        await update.message.reply_text(
            f"🎯 Current difficulty: <b>{game.difficulty.upper()}</b>\n\n"
            "🟢 <b>Easy</b>: 3-10 letters, slower progression\n"
            "🟡 <b>Medium</b>: 3-15 letters, moderate progression\n"
            "🔴 <b>Hard</b>: 4-20 letters, fast progression\n\n"
            "Use: /difficulty [easy/medium/hard]",
            parse_mode='HTML'
        )
        return
    
    new_diff = context.args[0].lower()
    if game.set_difficulty(new_diff):
        difficulty_emoji = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
        await update.message.reply_text(
            f"✅ Difficulty set to {difficulty_emoji[new_diff]} <b>{new_diff.upper()}</b>!",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Invalid difficulty! Use: easy, medium, or hard")

async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    user = update.effective_user
    current_player = game.players[game.current_player_index]
    
    if user.id != current_player['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    if not game.can_use_hint(user.id):
        time_left = int(HINT_COOLDOWN - (time.time() - game.player_hints.get(user.id, 0)))
        await update.message.reply_text(f"⏳ Hint on cooldown! Wait {time_left} seconds.")
        return
    
    hint_words = game.get_hint_words()
    if not hint_words:
        await update.message.reply_text("😅 No valid words found! Try a different approach.")
        return
    
    game.use_hint(user.id)
    db.ensure_player_exists(user.id, user.first_name)
    db.update_hint_skip(user.id, is_hint=True)
    
    await update.message.reply_text(
        f"💡 <b>Hint</b> - Try one of these:\n"
        f"{', '.join(hint_words)}",
        parse_mode='HTML'
    )

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    user = update.effective_user
    current_player = game.players[game.current_player_index]
    
    if user.id != current_player['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    if not game.can_skip(user.id):
        skips_left = game.skips_remaining.get(user.id, 0)
        if skips_left <= 0:
            await update.message.reply_text("❌ No skips remaining!")
        else:
            time_left = int(SKIP_COOLDOWN - (time.time() - game.player_skips.get(user.id, 0)))
            await update.message.reply_text(f"⏳ Skip on cooldown! Wait {time_left} seconds.")
        return
    
    game.use_skip(user.id)
    game.reset_streak(user.id)
    db.ensure_player_exists(user.id, user.first_name)
    db.update_hint_skip(user.id, is_hint=False)
    
    game.next_turn()
    next_player = game.players[game.current_player_index]
    skips_left = game.skips_remaining[user.id]
    
    await update.message.reply_text(
        f"⏭️ <b>{user.first_name}</b> skipped! ({skips_left} skips left)\n\n"
        f"👉 <b>{next_player['name']}</b>'s Turn.\n"
        f"Target: <b>{game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>",
        parse_mode='HTML'
    )

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db.get_player_stats(user.id)
    
    if not stats:
        await update.message.reply_text("📊 You haven't played any games yet! Join a /lobby to start.")
        return
    
    await update.message.reply_text(
        f"📊 <b>{user.first_name}'s Stats</b>\n\n"
        f"🎯 Total Score: <b>{stats[7]}</b>\n"
        f"📝 Words Played: <b>{stats[2]}</b>\n"
        f"📏 Avg Word Length: <b>{stats[10]:.1f}</b>\n"
        f"🏆 Longest Word: <b>{stats[4]}</b> ({stats[5]} letters)\n"
        f"🔥 Best Streak: <b>{stats[6]}</b>\n"
        f"💡 Hints Used: <b>{stats[8]}</b>\n"
        f"⏭️ Skips Used: <b>{stats[9]}</b>",
        parse_mode='HTML'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games or not update.message or not update.message.text: return
    
    game = games[chat_id]
    if not game.is_running: return

    user = update.effective_user
    current_player = game.players[game.current_player_index]

    if user.id != current_player['id']: return 

    word = update.message.text.strip().lower()

    if len(word) != game.current_word_length:
        await update.message.reply_text(f"❌ Length must be {game.current_word_length}! Try again.")
        return

    if not word.startswith(game.current_start_letter):
        await update.message.reply_text(f"❌ Must start with '{game.current_start_letter.upper()}'! Try again.")
        return

    if word in game.used_words:
        await update.message.reply_text("❌ Word already used! Try another.")
        return

    if word not in game.dictionary:
        await update.message.reply_text("❌ Not in my dictionary! Try again.")
        return

    game.used_words.add(word)
    game.increment_streak(user.id)
    current_streak = game.get_streak(user.id)
    
    db.update_word_stats(user.id, user.first_name, word, current_streak)
    
    streak_bonus = ""
    if current_streak >= 3:
        streak_bonus = f"\n🔥 <b>{current_streak} STREAK!</b> You're on fire!"
    
    game.next_turn()
    next_player = game.players[game.current_player_index]
    
    await update.message.reply_text(
        f"✅ <b>{user.first_name}</b> - '{word}' (+{len(word)} pts){streak_bonus}\n\n"
        f"👉 <b>{next_player['name']}</b>'s Turn\n"
        f"Target: <b>{game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>",
        parse_mode='HTML'
    )

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == '__main__':
    if BOT_TOKEN == "REPLACE_WITH_TOKEN_IF_NOT_USING_SECRETS":
        print("ERROR: Please set up the BOT_TOKEN in Secrets or paste it in the code.")
    else:
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("lobby", lobby))
        application.add_handler(CommandHandler("join", join))
        application.add_handler(CommandHandler("begin", begin_game))
        application.add_handler(CommandHandler("difficulty", difficulty))
        application.add_handler(CommandHandler("stop", stop_game))
        application.add_handler(CommandHandler("hint", hint_command))
        application.add_handler(CommandHandler("skip", skip_command))
        application.add_handler(CommandHandler("mystats", mystats_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

        logger.info(f"Loaded {len(games[list(games.keys())[0]].dictionary) if games else 'dictionary'} words")
        print("🎮 Bot is running with enhanced features!")
        application.run_polling()