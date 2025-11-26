import logging
import random
import sqlite3
import os
import asyncio
import time
from typing import List, Dict, Set, Optional
from datetime import datetime, timedelta
from threading import Thread

# Imports from the library
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# Flask for UptimeRobot pings
from flask import Flask

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "REPLACE_WITH_TOKEN_IF_NOT_USING_SECRETS")

# Files
DICTIONARY_FILE = "words.txt"
DB_FILE = "wordgame_leaderboard.db"

# Game Settings
TURN_TIMEOUT = 60

# Difficulty settings
DIFFICULTY_MODES = {
    'easy': {'start_length': 3, 'increment_every': 3, 'max_length': 10},
    'medium': {'start_length': 3, 'increment_every': 2, 'max_length': 15},
    'hard': {'start_length': 4, 'increment_every': 1, 'max_length': 20}
}

# Shop Boosts
SHOP_BOOSTS = {
    'hint': {'price': 80, 'description': '📖 Get dictionary meaning of a potential correct word'},
    'skip': {'price': 150, 'description': '⏭️ Skip your turn'},
    'rebound': {'price': 250, 'description': '🔄 Skip & pass same question to next player'}
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

        # Removed hints_used and skips_used columns
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
                average_word_length REAL DEFAULT 0.0
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER PRIMARY KEY,
                hint_count INTEGER DEFAULT 0,
                skip_count INTEGER DEFAULT 0,
                rebound_count INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0
            )
        ''')
        
        # Add balance column if it doesn't exist (migration)
        try:
            c.execute("ALTER TABLE inventory ADD COLUMN balance INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        conn.commit()
        conn.close()

    def update_word_stats(self, user_id, username, word, streak=0, forfeit=False):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()

        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        entry = c.fetchone()

        if forfeit:
            if entry:
                total_score = max(0, entry[7] - 10)
                c.execute('''UPDATE leaderboard SET 
                    total_score = ?
                    WHERE user_id=?''', (total_score, user_id))
            conn.commit()
            conn.close()
            return

        if entry:
            total_words = entry[2] + 1
            longest_word = entry[4] if len(entry[4]) > len(word) else word
            longest_word_length = max(entry[5], len(word))
            best_streak = max(entry[6], streak)
            total_score = entry[7] + len(word)
            avg_word_length = ((entry[8] * entry[2]) + len(word)) / total_words

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

        # Add points to shop balance (currency)
        c.execute("SELECT * FROM inventory WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO inventory (user_id, balance) VALUES (?, ?)", (user_id, len(word)))
        else:
            c.execute("UPDATE inventory SET balance = balance + ? WHERE user_id=?", (len(word), user_id))

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
    
    def get_balance(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT balance FROM inventory WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 0
    
    def get_inventory(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM inventory WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        if result: return {'hint': result[1], 'skip': result[2], 'rebound': result[3]}
        return {'hint': 0, 'skip': 0, 'rebound': 0}
    
    def buy_boost(self, user_id, boost_type, price):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        balance = self.get_balance(user_id)
        if balance < price: return False
        
        c.execute("SELECT * FROM inventory WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO inventory (user_id) VALUES (?)", (user_id,))
        
        c.execute("UPDATE inventory SET balance = balance - ? WHERE user_id=?", (price, user_id))
        col = f"{boost_type}_count"
        c.execute(f"UPDATE inventory SET {col} = {col} + 1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return True
    
    def use_boost(self, user_id, boost_type):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        col = f"{boost_type}_count"
        c.execute(f"UPDATE inventory SET {col} = {col} - 1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
    
    def add_balance(self, user_id, points):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM inventory WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO inventory (user_id, balance) VALUES (?, ?)", (user_id, points))
        else:
            c.execute("UPDATE inventory SET balance = balance + ? WHERE user_id=?", (points, user_id))
        conn.commit()
        conn.close()

# ==========================================
# GAME LOGIC
# ==========================================
class GameState:
    def __init__(self, chat_id=None, application=None):
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
        self.eliminated_players: Set[int] = set()
        self.last_word_length = 3
        self.difficulty_level = 0

        self.turn_start_time: Optional[float] = None
        self.timeout_task: Optional[asyncio.Task] = None
        self.chat_id = chat_id
        self.application = application
        self.current_turn_user_id: Optional[int] = None
        
        self.rebound_target_letter: Optional[str] = None
        self.rebound_target_length: Optional[int] = None

        self.load_dictionary()

    def load_dictionary(self):
        if os.path.exists(DICTIONARY_FILE):
            try:
                with open(DICTIONARY_FILE, 'r', encoding='utf-8') as f:
                    self.dictionary = {line.strip().lower() for line in f}
                logger.info(f"Loaded {len(self.dictionary)} words from {DICTIONARY_FILE}")
            except Exception as e:
                logger.error(f"Error loading dictionary: {e}")
                self.use_fallback_dictionary()
        else:
            logger.warning("Dictionary file not found. Using fallback list.")
            self.use_fallback_dictionary()

    def use_fallback_dictionary(self):
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
        self.eliminated_players = set()
        self.last_word_length = difficulty_config['start_length']
        self.difficulty_level = 0
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

        difficulty_increased = False
        if self.turn_count > 0 and self.turn_count % (len(self.players) * increment_every) == 0:
            self.current_word_length += 1
            self.difficulty_level += 1
            difficulty_increased = True
            if self.current_word_length > max_length:
                self.current_word_length = max_length

        import string
        self.current_start_letter = random.choice(string.ascii_lowercase)
        self.turn_start_time = time.time()
        self.last_word_length = self.current_word_length
        return difficulty_increased
    
    def get_turn_time(self) -> int:
        base_time = 60
        time_reduction = self.difficulty_level * 5
        return max(20, base_time - time_reduction)
    
    def cancel_timeout(self):
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
            self.timeout_task = None

    def get_streak(self, user_id: int) -> int:
        return self.player_streaks.get(user_id, 0)

    def increment_streak(self, user_id: int):
        self.player_streaks[user_id] = self.player_streaks.get(user_id, 0) + 1

    def reset_streak(self, user_id: int):
        self.player_streaks[user_id] = 0

    # Removed can_use_hint, use_hint, can_skip, use_skip, get_hint_words methods

    def initialize_player_stats(self, user_id: int):
        if user_id not in self.player_streaks:
            self.player_streaks[user_id] = 0

# Key: chat_id, Value: GameState
games: Dict[int, GameState] = {}
db = DatabaseManager(DB_FILE)

async def handle_turn_timeout(chat_id: int, user_id: int, application):
    await asyncio.sleep(games[chat_id].get_turn_time())
    
    if chat_id not in games or not games[chat_id].is_running:
        return
    
    game = games[chat_id]
    current_player = game.players[game.current_player_index]
    
    if current_player['id'] != user_id:
        return
    
    game.eliminated_players.add(user_id)
    game.reset_streak(user_id)
    db.update_word_stats(user_id, current_player['name'], "", 0, forfeit=True)
    
    await application.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ *Time's Up\\!* @{current_player['username']} is eliminated\\! \\(-10 pts\\)\n\nPoints before elimination count\\.",
        parse_mode='MarkdownV2'
    )
    
    game.next_turn()
    
    if len(game.eliminated_players) >= len(game.players) - 1:
        winner = next((p for p in game.players if p['id'] not in game.eliminated_players), None)
        if winner:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"🏆 *GAME OVER\\!*\n\n👑 *Winner:* @{winner['username']}",
                parse_mode='MarkdownV2'
            )
        game.reset()
        return
    
    next_player = game.players[game.current_player_index]
    while next_player['id'] in game.eliminated_players:
        game.next_turn()
        next_player = game.players[game.current_player_index]
    
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    
    await application.bot.send_message(
        chat_id=chat_id,
        text=f"👉 @{next_player['username']}'s Turn\n"
             f"Target: *{game.current_word_length} letters* starting with *{game.current_start_letter.upper()}*\n"
             f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )
    
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], application))

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
        "/forfeit - Give up your turn (-10 pts, points before forfeit count)\n"
        "/stop - Stop the current game\n\n"
        "💰 <b>Shop & Boosts:</b>\n"
        "/shop - View available boosts\n"
        "/buy_hint /buy_skip /buy_rebound - Purchase boosts\n"
        "/hint - Get word suggestions\n"
        "/skip_boost - Skip without penalty\n"
        "/rebound - Skip & pass question to next player\n\n"
        "📊 <b>Stats & Leaderboard:</b>\n"
        "/mystats - View your personal stats\n"
        "/leaderboard [score/words/streak/longest] - Top players\n\n"
        "💡 <b>Features:</b>\n"
        "• Streak tracking & combo bonuses\n"
        "• Three difficulty modes\n"
        "• Comprehensive player statistics\n"
        "• Shop system with purchasable boosts\n",
        parse_mode='HTML'
    )

async def lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        games[chat_id] = GameState(chat_id=chat_id, application=context.application)
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
    username = user.username if user.username else user.first_name
    game.players.append({'id': user.id, 'name': user.first_name, 'username': username})

    await update.message.reply_text(
        f"📢 <b>Lobby Opened!</b>\n\n"
        f"@{username} has joined.\n"
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
        await update.message.reply_text(f"👤 You are already in.")
        return

    username = user.username if user.username else user.first_name
    game.players.append({'id': user.id, 'name': user.first_name, 'username': username})
    game.initialize_player_stats(user.id)
    await update.message.reply_text(f"✅ @{username} joined! (Total: {len(game.players)})", parse_mode='HTML')

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
    turn_time = game.get_turn_time()
    game.current_turn_user_id = current_player['id']

    difficulty_emoji = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
    await update.message.reply_text(
        f"🎮 *Game Started\\!*\n"
        f"Difficulty: {difficulty_emoji.get(game.difficulty, '🟡')} *{game.difficulty.upper()}*\n"
        f"Players: {', '.join([p['username'] for p in game.players])}\n\n"
        f"👉 @{current_player['username']}'s turn\\!\n"
        f"Write a *{game.current_word_length}\\-letter* word starting with *'{game.current_start_letter.upper()}'*\n"
        f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )
    
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, current_player['id'], context.application))

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
        games[chat_id] = GameState(chat_id=chat_id, application=context.application)

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

async def forfeit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    game.cancel_timeout()
    game.eliminated_players.add(user.id)
    game.reset_streak(user.id)
    db.update_word_stats(user.id, user.first_name, "", 0, forfeit=True)
    
    await update.message.reply_text(f"⛔ <b>You forfeited!</b> (-10 pts)\n\nYour accumulated points are valid.", parse_mode='HTML')
    
    game.next_turn()
    
    if len(game.eliminated_players) >= len(game.players) - 1:
        winner = next((p for p in game.players if p['id'] not in game.eliminated_players), None)
        if winner:
            await update.message.reply_text(f"🏆 *GAME OVER\\!*\n\n👑 *Winner:* @{winner['username']}", parse_mode='MarkdownV2')
        game.reset()
        return
    
    next_player = game.players[game.current_player_index]
    while next_player['id'] in game.eliminated_players:
        game.next_turn()
        next_player = game.players[game.current_player_index]
    
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    await update.message.reply_text(
        f"👉 @{next_player['username']}'s Turn\n"
        f"Target: *{game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n"
        f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )
    
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))

async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    balance = db.get_balance(user.id)
    inventory = db.get_inventory(user.id)
    
    text = f"🛍️ <b>SHOP</b> 💰 Balance: <b>{balance} pts</b>\n\n"
    for boost_type, details in SHOP_BOOSTS.items():
        owned = inventory[boost_type]
        text += f"{details['description']}\n💵 Price: <b>{details['price']} pts</b> - Owned: <b>{owned}</b>\n/buy_{boost_type}\n\n"
    text += "Example: /buy_hint to purchase hint boost"
    await update.message.reply_text(text, parse_mode='HTML')

async def buy_boost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message_text = update.message.text.lower()
    
    boost_type = None
    for boost in SHOP_BOOSTS.keys():
        if f"/buy_{boost}" in message_text:
            boost_type = boost
            break
    
    if not boost_type:
        await update.message.reply_text("❌ Invalid boost! Use: /buy_hint, /buy_skip, or /buy_rebound")
        return
    
    price = SHOP_BOOSTS[boost_type]['price']
    if db.buy_boost(user.id, boost_type, price):
        await update.message.reply_text(f"✅ Purchased {boost_type}! (-{price} pts)")
    else:
        balance = db.get_balance(user.id)
        await update.message.reply_text(f"❌ Insufficient balance! Need {price} pts, have {balance} pts")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db.get_player_stats(user.id)

    if not stats:
        await update.message.reply_text("📊 You haven't played any games yet! Join a /lobby to start.")
        return

    stats_text = (
        f"📊 <b>{user.first_name}'s Stats</b>\n\n"
        f"🎯 Total Score: <b>{stats[7]}</b>\n"
        f"📝 Words Played: <b>{stats[2]}</b>\n"
        f"📏 Avg Word Length: <b>{stats[8]:.1f}</b>\n"
        f"🏆 Longest Word: <b>{stats[4]}</b> ({stats[5]} letters)\n"
        f"🔥 Best Streak: <b>{stats[6]}</b>"
    )

    try:
        profile_photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if profile_photos.photos:
            photo = profile_photos.photos[0][-1]
            await update.message.reply_photo(photo=photo, caption=stats_text, parse_mode='HTML')
        else:
            await update.message.reply_text(stats_text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error fetching profile photo: {str(e)}")
        await update.message.reply_text(stats_text, parse_mode='HTML')

async def hint_boost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    if user.id != game.players[game.current_player_index]['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    inventory = db.get_inventory(user.id)
    if inventory['hint'] <= 0:
        await update.message.reply_text(f"❌ No hint boosts! Buy one for {SHOP_BOOSTS['hint']['price']} pts")
        return
    
    words = [w for w in game.dictionary if len(w) == game.current_word_length and w.startswith(game.current_start_letter)][:3]
    if words:
        db.use_boost(user.id, 'hint')
        text = f"📖 *Hint\\!* Possible words: {', '.join(words)}"
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    else:
        await update.message.reply_text("❌ No valid words found!")

async def skip_boost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    if user.id != game.players[game.current_player_index]['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    inventory = db.get_inventory(user.id)
    if inventory['skip'] <= 0:
        await update.message.reply_text(f"❌ No skip boosts! Buy one for {SHOP_BOOSTS['skip']['price']} pts")
        return
    
    db.use_boost(user.id, 'skip')
    game.cancel_timeout()
    game.next_turn()
    next_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    
    await update.message.reply_text(f"⏭️ @{user.username} used skip boost\\!\n\n👉 @{next_player['username']}'s Turn\nTarget: *{game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n⏱️ *Time: {turn_time}s*", parse_mode='MarkdownV2')
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))

async def rebound_boost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    if user.id != game.players[game.current_player_index]['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    inventory = db.get_inventory(user.id)
    if inventory['rebound'] <= 0:
        await update.message.reply_text(f"❌ No rebound boosts! Buy one for {SHOP_BOOSTS['rebound']['price']} pts")
        return
    
    db.use_boost(user.id, 'rebound')
    game.cancel_timeout()
    game.rebound_target_letter = game.current_start_letter
    game.rebound_target_length = game.current_word_length
    game.next_turn()
    next_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    
    await update.message.reply_text(f"🔄 @{user.username} rebounded\\!\n\n👉 @{next_player['username']}'s Turn \\(SAME QUESTION\\)\nTarget: *{game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n⏱️ *Time: {turn_time}s*", parse_mode='MarkdownV2')
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))

async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    inventory = db.get_inventory(user.id)
    balance = db.get_balance(user.id)
    
    text = f"📦 <b>{user.first_name}'s Inventory</b>\n\n"
    text += f"💰 Balance: <b>{balance} pts</b>\n\n"
    text += "<b>Boosts Owned:</b>\n"
    text += f"📖 Hints: <b>{inventory['hint']}</b>\n"
    text += f"⏭️ Skips: <b>{inventory['skip']}</b>\n"
    text += f"🔄 Rebounds: <b>{inventory['rebound']}</b>\n\n"
    text += "Visit /shop to buy more boosts!"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def omnipotent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ This command only works in group chats!")
        return
    
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = chat_member.status in ['creator', 'administrator']
    except:
        is_admin = False
    
    if not is_admin:
        await update.message.reply_text("❌ Only group admins can use /omnipotent!")
        return
    
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("❌ Reply to a user's message with /omnipotent [points]\nExample: Reply to their message with /omnipotent 100")
        return
    
    target_user = update.message.reply_to_message.from_user
    points = 0
    
    if context.args and context.args[0].isdigit():
        points = int(context.args[0])
    else:
        await update.message.reply_text("❌ Usage: Reply to a message with /omnipotent [points]\nExample: /omnipotent 100")
        return
    
    if points <= 0:
        await update.message.reply_text("❌ Points must be greater than 0!")
        return
    
    db.add_balance(target_user.id, points)
    await update.message.reply_text(f"✨ @{target_user.username} received <b>+{points} pts</b> from <b>@{user.username}</b> (Admin Gift)!", parse_mode='HTML')

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

    try:
        game.cancel_timeout()
        game.used_words.add(word)
        game.increment_streak(user.id)
        current_streak = game.get_streak(user.id)

        db.update_word_stats(user.id, user.first_name, word, current_streak)

        streak_bonus = ""
        if current_streak >= 3:
            streak_bonus = f"\n🔥 *{current_streak} STREAK\\!* You're on fire\\!"

        difficulty_increased = game.next_turn()
        
        msg_text = f"✅ '{word}' \\(\\+{len(word)} pts\\){streak_bonus}\n\n"
        
        if difficulty_increased:
            msg_text += f"📈 *DIFFICULTY INCREASED\\!* Now *{game.current_word_length} letters\\!*\n\n"
        
        next_player = game.players[game.current_player_index]
        turn_time = game.get_turn_time()
        game.current_turn_user_id = next_player['id']
        
        msg_text += f"👉 @{next_player['username']}'s Turn\n"
        msg_text += f"Target: *{game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n"
        msg_text += f"⏱️ *Time: {turn_time}s*"

        await update.message.reply_text(msg_text, parse_mode='MarkdownV2')
        game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))
    except Exception as e:
        logger.error(f"Error processing word '{word}': {str(e)}", exc_info=True)
        await update.message.reply_text(f"❌ Error processing your word. Try again.")
        game.used_words.discard(word)

# ==========================================
# WEB SERVER FOR UPTIMEBOT PINGS
# ==========================================
import socket

def run_web_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', 8080))
    sock.listen(5)
    sock.settimeout(1.0)
    
    while True:
        try:
            client, addr = sock.accept()
            try:
                request = client.recv(1024).decode('utf-8', errors='ignore')
                if 'GET' in request:
                    response = b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 12\r\nConnection: close\r\n\r\nBot is alive'
                    client.send(response)
            except:
                pass
            finally:
                client.close()
        except socket.timeout:
            continue
        except Exception as e:
            logger.error(f"Server error: {e}")
            break

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == '__main__':
    if BOT_TOKEN == "REPLACE_WITH_TOKEN_IF_NOT_USING_SECRETS":
        print("ERROR: Please set up the BOT_TOKEN in Secrets or paste it in the code.")
    else:
        # Start web server in background thread for UptimeRobot
        web_thread = Thread(target=run_web_server, daemon=True)
        web_thread.start()
        print("✅ Web server started on port 8080 for UptimeRobot pings")
        
        while True:
            try:
                application = ApplicationBuilder().token(BOT_TOKEN).build()
                
                application.add_handler(CommandHandler("start", start))
                application.add_handler(CommandHandler("lobby", lobby))
                application.add_handler(CommandHandler("join", join))
                application.add_handler(CommandHandler("begin", begin_game))
                application.add_handler(CommandHandler("difficulty", difficulty))
                application.add_handler(CommandHandler("stop", stop_game))
                application.add_handler(CommandHandler("forfeit", forfeit_command))
                application.add_handler(CommandHandler("mystats", mystats_command))
                application.add_handler(CommandHandler("leaderboard", leaderboard))
                application.add_handler(CommandHandler("shop", shop_command))
                application.add_handler(CommandHandler("buy_hint", buy_boost_command))
                application.add_handler(CommandHandler("buy_skip", buy_boost_command))
                application.add_handler(CommandHandler("buy_rebound", buy_boost_command))
                application.add_handler(CommandHandler("hint", hint_boost_command))
                application.add_handler(CommandHandler("skip_boost", skip_boost_command))
                application.add_handler(CommandHandler("rebound", rebound_boost_command))
                application.add_handler(CommandHandler("inventory", inventory_command))
                application.add_handler(CommandHandler("omnipotent", omnipotent_command))
                application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

                logger.info(f"Loaded dictionary words")
                print("🎮 Bot is running with enhanced features!")
                application.run_polling()
            except KeyboardInterrupt:
                print("Bot stopped by user.")
                break
            except Exception as e:
                logger.error(f"Bot crashed: {str(e)}", exc_info=True)
                print(f"Bot encountered an error: {e}. Restarting in 5 seconds...")
                time.sleep(5)