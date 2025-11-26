import logging
import random
import sqlite3
import os
import asyncio
import time
import subprocess
import signal
import sys
from typing import List, Dict, Set, Optional
from datetime import datetime, timedelta
from threading import Thread
import atexit
from io import BytesIO

# Imports from the library
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

# Bot Owner (for exclusive KAMI title) - Set via environment variable or hardcode here
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))  # Set BOT_OWNER_ID env var to your Telegram user ID

# Available Titles with Dynamic Requirements
TITLES = {
    'legend': {'display': '👑 LEGEND 👑', 'color': '🟡', 'requirement': 'total_score >= 1000'},
    'warrior': {'display': '⚔️ WARRIOR ⚔️', 'color': '🔴', 'requirement': 'best_streak >= 10'},
    'sage': {'display': '🧙 SAGE 🧙', 'color': '🟣', 'requirement': 'total_words >= 50'},
    'phoenix': {'display': '🔥 PHOENIX 🔥', 'color': '🟠', 'requirement': 'games_played >= 10'},
    'shadow': {'display': '🌑 SHADOW 🌑', 'color': '⚫', 'requirement': 'longest_word_length >= 12'},
    'kami': {'display': '✨ KAMI ✨', 'color': '💎', 'exclusive': True}
}

# Title Requirements (matched to title themes)
TITLE_REQUIREMENTS = {
    'legend': {'total_score': 1000, 'desc': '👑 Reach 1000 total points'},
    'warrior': {'best_streak': 10, 'desc': '⚔️ Achieve 10+ word streak'},
    'sage': {'total_words': 50, 'desc': '🧙 Submit 50+ words'},
    'phoenix': {'games_played': 10, 'desc': '🔥 Complete 10+ games (rebirth)'},
    'shadow': {'longest_word_length': 12, 'desc': '🌑 Find a 12+ letter word (hidden)'},
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
        
        # Create titles table
        c.execute('''
            CREATE TABLE IF NOT EXISTS titles (
                user_id INTEGER PRIMARY KEY,
                active_title TEXT DEFAULT '',
                unlocked_titles TEXT DEFAULT ''
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_active_title(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT active_title FROM titles WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else ''
    
    def set_active_title(self, user_id, title):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM titles WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO titles (user_id, active_title) VALUES (?, ?)", (user_id, title))
        else:
            c.execute("UPDATE titles SET active_title = ? WHERE user_id=?", (title, user_id))
        conn.commit()
        conn.close()
    
    def unlock_title(self, user_id, title):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT unlocked_titles FROM titles WHERE user_id=?", (user_id,))
        result = c.fetchone()
        if not result:
            c.execute("INSERT INTO titles (user_id, unlocked_titles) VALUES (?, ?)", (user_id, title))
        else:
            unlocked = set(result[0].split(',')) if result[0] else set()
            unlocked.add(title)
            c.execute("UPDATE titles SET unlocked_titles = ? WHERE user_id=?", (','.join(unlocked), user_id))
        conn.commit()
        conn.close()
    
    def get_unlocked_titles(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT unlocked_titles FROM titles WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return set(result[0].split(',')) if result and result[0] else set()
    
    def check_title_unlock(self, user_id, title):
        if title not in TITLE_REQUIREMENTS:
            return True
        stats = self.get_player_stats(user_id)
        if not stats:
            return False
        reqs = TITLE_REQUIREMENTS[title]
        checks = {
            'total_score': stats[7] >= reqs.get('total_score', float('inf')),
            'best_streak': stats[6] >= reqs.get('best_streak', float('inf')),
            'total_words': stats[2] >= reqs.get('total_words', float('inf')),
            'games_played': stats[3] >= reqs.get('games_played', float('inf')),
            'longest_word_length': stats[5] >= reqs.get('longest_word_length', float('inf'))
        }
        return all(checks.values())
    
    def auto_unlock_titles(self, user_id):
        """Auto-unlock all titles the user qualifies for based on current stats. Returns newly unlocked titles."""
        unlocked = self.get_unlocked_titles(user_id)
        newly_unlocked = []
        for title_key in TITLE_REQUIREMENTS.keys():
            if title_key not in unlocked and self.check_title_unlock(user_id, title_key):
                self.unlock_title(user_id, title_key)
                newly_unlocked.append(title_key)
        return newly_unlocked

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
    
    def increment_games_played(self, user_id):
        """Increment games_played counter when a game is completed"""
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        entry = c.fetchone()
        
        if entry:
            new_games_played = entry[3] + 1
            c.execute("UPDATE leaderboard SET games_played = ? WHERE user_id=?", 
                     (new_games_played, user_id))
        else:
            c.execute("INSERT INTO leaderboard (user_id, games_played) VALUES (?, 1)", 
                     (user_id,))
        
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
        
        self.group_owner: Optional[int] = None
        self.booster_limits = {'hint': float('inf'), 'skip': float('inf'), 'rebound': float('inf')}
        self.booster_usage = {'hint': 0, 'skip': 0, 'rebound': 0}
        self.is_practice: bool = False
        self.last_activity_time: float = time.time()  # Track for memory cleanup

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
        self.group_owner = None
        self.booster_limits = {'hint': float('inf'), 'skip': float('inf'), 'rebound': float('inf')}
        self.booster_usage = {'hint': 0, 'skip': 0, 'rebound': 0}
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

# ==========================================
# STALE MESSAGE FILTERING & RATE LIMITING & CLEANUP
# ==========================================
BOT_START_TIME = time.time()  # Track when bot starts to filter old messages
STALE_MESSAGE_THRESHOLD = 5  # Ignore messages older than 5 seconds from now
user_command_cooldowns: Dict[int, Dict[str, float]] = {}  # {user_id: {command: last_time}}
COMMAND_COOLDOWN_SECONDS = 1  # 1 second between commands per user
GAME_CLEANUP_INTERVAL = 3600  # Clean up games every hour

def is_message_stale(update: Update) -> bool:
    """Check if a message was sent before bot started (prevents processing offline messages)"""
    if not update.message or not update.message.date:
        return False
    
    message_timestamp = update.message.date.timestamp()
    current_time = time.time()
    
    # Ignore messages older than the threshold
    if current_time - message_timestamp > STALE_MESSAGE_THRESHOLD:
        return True
    
    return False

async def cleanup_old_games():
    """Periodically remove completed games from memory to prevent memory leaks"""
    while True:
        try:
            await asyncio.sleep(GAME_CLEANUP_INTERVAL)
            current_time = time.time()
            games_to_delete = []
            
            for chat_id, game in games.items():
                # Remove games that are not running and haven't been touched for 1 hour
                if not game.is_running and not game.is_lobby_open:
                    if hasattr(game, 'last_activity_time'):
                        if current_time - game.last_activity_time > GAME_CLEANUP_INTERVAL:
                            games_to_delete.append(chat_id)
                    else:
                        games_to_delete.append(chat_id)
            
            for chat_id in games_to_delete:
                del games[chat_id]
                logger.info(f"Cleaned up game state for chat {chat_id}")
        except Exception as e:
            logger.error(f"Error in game cleanup task: {e}")

def check_rate_limit(user_id: int, command: str) -> bool:
    """Check if user has exceeded command rate limit"""
    current_time = time.time()
    
    if user_id not in user_command_cooldowns:
        user_command_cooldowns[user_id] = {}
    
    if command in user_command_cooldowns[user_id]:
        last_use = user_command_cooldowns[user_id][command]
        if current_time - last_use < COMMAND_COOLDOWN_SECONDS:
            return False
    
    user_command_cooldowns[user_id][command] = current_time
    return True

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
    
    if not game.is_practice:
        db.update_word_stats(user_id, current_player['name'], "", 0, forfeit=True)
    
    if game.is_practice:
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>TIME'S UP!</b>\n\n❌ You were eliminated due to timeout!\n\n(Practice mode - no points deducted)",
            parse_mode='HTML'
        )
    else:
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>TIME'S UP!</b>\n\n❌ @{current_player['username']} is eliminated due to timeout!\n\n<i>Forfeit - points earned before timeout still count.</i>",
            parse_mode='HTML'
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
            # Increment games_played for all participants
            for player in game.players:
                db.increment_games_played(player['id'])
        game.reset()
        return
    
    # Find next non-eliminated player with safety check
    next_player = game.players[game.current_player_index]
    max_iterations = len(game.players)
    iterations = 0
    while next_player['id'] in game.eliminated_players and iterations < max_iterations:
        game.next_turn()
        next_player = game.players[game.current_player_index]
        iterations += 1
    
    # Verify we found a valid player
    if next_player['id'] in game.eliminated_players:
        # Increment games_played for all participants before reset
        for player in game.players:
            db.increment_games_played(player['id'])
        game.reset()
        await application.bot.send_message(
            chat_id=chat_id,
            text="❌ Game error: No valid players remaining. Game reset.",
            parse_mode='MarkdownV2'
        )
        return
    
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
    if is_message_stale(update):
        return
    
    user = update.effective_user
    
    # Give KAMI title only to configured owner
    if BOT_OWNER_ID > 0 and user.id == BOT_OWNER_ID:
        db.unlock_title(user.id, 'kami')
    
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
        "🏆 <b>Achievements & Titles:</b>\n"
        "/achievements - View all available titles\n"
        "/settitle [title] - Set your active title\n"
        "/mytitle - View your current title\n\n"
        "💡 <b>Features:</b>\n"
        "• Streak tracking & combo bonuses\n"
        "• Three difficulty modes\n"
        "• Comprehensive player statistics\n"
        "• Stylized achievement titles\n",
        parse_mode='HTML'
    )

async def lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update):
        return
    
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
    game.group_owner = update.effective_user.id

    user = update.effective_user
    display_name = str(user.first_name or user.username or "Player").strip()
    if not display_name or display_name == "None":
        display_name = "Player"
    username_to_store = (user.username if user.username else display_name).lstrip('@')
    game.players.append({'id': user.id, 'name': display_name, 'username': username_to_store})
    db.ensure_player_exists(user.id, username_to_store)

    await update.message.reply_text(
        f"📢 <b>Lobby Opened!</b>\n\n"
        f"{display_name} has joined.\n"
        f"Waiting for others... Type /join to play!",
        parse_mode='HTML'
    )

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update):
        return
    
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in games or not games[chat_id].is_lobby_open:
        await update.message.reply_text("❌ No lobby open. Type /lobby to start one.")
        return

    game = games[chat_id]

    if any(p['id'] == user.id for p in game.players):
        await update.message.reply_text(f"👤 You are already in.")
        return

    display_name = str(user.first_name or user.username or "Player").strip()
    if not display_name or display_name == "None":
        display_name = "Player"
    username_to_store = (user.username if user.username else display_name).lstrip('@')
    game.players.append({'id': user.id, 'name': display_name, 'username': username_to_store})
    game.initialize_player_stats(user.id)
    db.ensure_player_exists(user.id, username_to_store)
    await update.message.reply_text(f"✅ {display_name} joined! (Total: {len(game.players)})", parse_mode='HTML')

async def begin_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update):
        return
    
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
    player_names = ', '.join([str(p['name']) for p in game.players if p.get('name')])
    await update.message.reply_text(
        f"🎮 *Game Started\\!*\n"
        f"Difficulty: {difficulty_emoji.get(game.difficulty, '🟡')} *{game.difficulty.upper()}*\n"
        f"Players: {player_names}\n\n"
        f"👉 {str(current_player['name'])}'s turn\\!\n"
        f"Write a *{game.current_word_length}\\-letter* word starting with *'{game.current_start_letter.upper()}'*\n"
        f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )
    
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, current_player['id'], context.application))

async def stop_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games:
        await update.message.reply_text("❌ No active game to stop.")
        return
    
    game = games[chat_id]
    
    # Check if user is the lobby creator or an admin
    is_lobby_creator = user.id == game.group_owner
    is_admin = False
    
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
    
    if not is_lobby_creator and not is_admin:
        await update.message.reply_text("❌ Only the lobby creator or admins can stop the game!")
        return
    
    game.reset()
    await update.message.reply_text("🛑 Game stopped by admin or lobby creator.")

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
    chat_id = update.effective_chat.id
    
    # Rate limiting
    if not check_rate_limit(user.id, 'shop'):
        return
    
    if chat_id in games and games[chat_id].is_running:
        await update.message.reply_text("❌ Cannot access shop during an active game! Finish the game first with /stop")
        return
    
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
    chat_id = update.effective_chat.id
    message_text = update.message.text.lower()
    
    # Rate limiting
    if not check_rate_limit(user.id, 'buy_boost'):
        return
    
    if chat_id in games and games[chat_id].is_running:
        await update.message.reply_text("❌ Cannot buy boosts during an active game! Finish the game first with /stop")
        return
    
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
    
    # Rate limiting
    if not check_rate_limit(user.id, 'mystats'):
        return
    
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
    
    # Rate limiting
    if not check_rate_limit(user.id, 'hint'):
        return
    
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    if user.id != game.players[game.current_player_index]['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    if game.booster_limits.get('hint', float('inf')) == -1:
        await update.message.reply_text("❌ Hint boosts are disabled for this game!")
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
    
    # Rate limiting
    if not check_rate_limit(user.id, 'skip'):
        return
    
    if chat_id not in games or not games[chat_id].is_running:
        await update.message.reply_text("❌ No active game!")
        return
    
    game = games[chat_id]
    if user.id != game.players[game.current_player_index]['id']:
        await update.message.reply_text("❌ It's not your turn!")
        return
    
    if game.booster_limits.get('skip', float('inf')) == -1:
        await update.message.reply_text("❌ Skip boosts are disabled for this game!")
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
    
    if game.booster_limits.get('rebound', float('inf')) == -1:
        await update.message.reply_text("❌ Rebound boosts are disabled for this game!")
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

async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    newly_unlocked = db.auto_unlock_titles(user.id)
    
    if newly_unlocked:
        unlock_msg = "🎉 <b>NEW TITLES UNLOCKED!</b>\n\n"
        for title_key in newly_unlocked:
            if title_key in TITLES:
                unlock_msg += f"✨ {TITLES[title_key]['display']}\n"
        await update.message.reply_text(unlock_msg, parse_mode='HTML')
    
    unlocked = db.get_unlocked_titles(user.id)
    active = db.get_active_title(user.id)
    stats = db.get_player_stats(user.id)
    
    text = "🏆 <b>Available Titles</b>\n\n"
    for title_key, title_data in TITLES.items():
        is_exclusive = title_data.get('exclusive', False)
        
        if is_exclusive and user.id != BOT_OWNER_ID:
            continue
        
        is_unlocked = title_key in unlocked or (is_exclusive and user.id == BOT_OWNER_ID)
        status = "🔓" if is_unlocked else "🔒"
        active_mark = "⭐" if title_key == active else ""
        
        text += f"{status} {title_data['display']} {active_mark}\n"
        
        if not is_unlocked and title_key in TITLE_REQUIREMENTS and stats:
            req = TITLE_REQUIREMENTS[title_key]
            text += f"  {req['desc']}\n"
        elif is_exclusive and user.id == BOT_OWNER_ID:
            text += "  (Exclusive Owner Title)\n"
    
    text += "\n/settitle [title] to activate\n/progress to see unlock status"
    await update.message.reply_text(text, parse_mode='HTML')

async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    newly_unlocked = db.auto_unlock_titles(user.id)
    
    if newly_unlocked:
        unlock_msg = "🎉 <b>NEW TITLES UNLOCKED!</b>\n\n"
        for title_key in newly_unlocked:
            if title_key in TITLES:
                unlock_msg += f"✨ {TITLES[title_key]['display']}\n"
        await update.message.reply_text(unlock_msg, parse_mode='HTML')
    
    stats = db.get_player_stats(user.id)
    
    if not stats:
        await update.message.reply_text("📊 No stats yet! Play some games to unlock titles.", parse_mode='HTML')
        return
    
    text = f"📊 <b>Your Progress</b>\n\n"
    text += f"🎯 Total Score: {stats[7]}/1000 ({int(stats[7]/10)}%)\n"
    text += f"⚔️ Best Streak: {stats[6]}/10 ({int(stats[6]/10*100)}%)\n"
    text += f"📝 Words: {stats[2]}/50 ({int(stats[2]/50*100)}%)\n"
    text += f"🔥 Games: {stats[3]}/10 ({int(stats[3]/10*100)}%)\n"
    text += f"🌑 Longest Word: {stats[5]}/12 letters\n\n"
    
    text += "<b>Unlocked Titles:</b>\n"
    unlocked = db.get_unlocked_titles(user.id)
    if user.id == BOT_OWNER_ID:
        unlocked.add('kami')
    
    if unlocked:
        for t in unlocked:
            if t in TITLES:
                text += f"✅ {TITLES[t]['display']}\n"
    else:
        text += "None yet. Keep playing!\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def settitle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /settitle [title]\nExample: /settitle legend")
        return
    
    title = context.args[0].lower()
    
    if title not in TITLES:
        await update.message.reply_text(f"❌ Title '{title}' doesn't exist!")
        return
    
    is_exclusive = TITLES[title].get('exclusive', False)
    
    if is_exclusive and user.id != BOT_OWNER_ID:
        await update.message.reply_text(f"❌ {TITLES[title]['display']} is exclusive to the bot owner!")
        return
    
    unlocked = db.get_unlocked_titles(user.id)
    
    if title not in unlocked:
        can_unlock = db.check_title_unlock(user.id, title)
        if not can_unlock:
            req = TITLE_REQUIREMENTS.get(title, {})
            await update.message.reply_text(f"❌ Requirements not met!\n{req.get('desc', '')}\n\nUse /progress to see your status")
            return
        db.unlock_title(user.id, title)
    
    db.set_active_title(user.id, title)
    await update.message.reply_text(f"✅ Title set to {TITLES[title]['display']}")

async def mytitle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    active = db.get_active_title(user.id)
    
    if not active or active not in TITLES:
        await update.message.reply_text("❌ You don't have an active title! Use /settitle [title]")
        return
    
    title_data = TITLES[active]
    await update.message.reply_text(f"👤 Your Title: {title_data['display']}", parse_mode='HTML')

async def practice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Me vs Me - Solo practice mode"""
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    
    difficulty = context.args[0].lower() if context.args else 'medium'
    if difficulty not in DIFFICULTY_MODES:
        await update.message.reply_text("❌ Invalid difficulty! Use: /practice easy/medium/hard")
        return
    
    if chat_id in games:
        game = games[chat_id]
        if game.is_running or game.is_lobby_open:
            await update.message.reply_text("❌ A game is already in progress! Use /stop first.")
            return
    
    game = GameState(chat_id=chat_id, application=context.application)
    game.set_difficulty(difficulty)
    game.is_running = True
    game.is_practice = True
    display_name = str(user.first_name or user.username or "Player").strip()
    if not display_name or display_name == "None":
        display_name = "Player"
    game.players = [{'id': user_id, 'name': display_name, 'username': user.username or display_name}]
    game.initialize_player_stats(user_id)
    games[chat_id] = game
    
    game.next_turn()
    turn_time = game.get_turn_time()
    game.current_turn_user_id = user_id
    
    difficulty_emoji = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
    await update.message.reply_text(
        f"🎮 <b>ME VS ME - PRACTICE MODE</b>\n"
        f"Difficulty: {difficulty_emoji.get(difficulty, '🟡')} <b>{difficulty.upper()}</b>\n\n"
        f"💪 Challenge yourself and build a streak!\n"
        f"Target: <b>{game.current_word_length}-letter</b> word starting with <b>'{game.current_start_letter.upper()}'</b>\n"
        f"⏱️ <b>Time: {turn_time}s</b>\n\n"
        f"Type your word below!",
        parse_mode='HTML'
    )
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, user_id, context.application))

async def groupdesc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display group chat description and rules"""
    group_description = """
🎮 <b>WORD GAME GROUP - RULES & DESCRIPTION</b>

📝 <b>About This Group:</b>
This is an interactive Telegram word game community! Players compete in turn-based word challenges to earn points, climb leaderboards, unlock achievements, and customize titles. Join lobbies, challenge friends, and build your gaming reputation!

🎯 <b>Main Commands:</b>
• /lobby - Start a new game
• /join - Join a lobby
• /begin - Start the game (2+ players)
• /leaderboard - See top players
• /mystats - Check your stats
• /profile - View player profiles

💬 <b>GROUP CHAT RULES:</b>
✅ <b>ALLOWED:</b>
• Friendly banter & competition
• Sharing wins & celebrating achievements
• General conversation between members
• Asking for game tips & strategies

❌ <b>STRICTLY PROHIBITED:</b>
• 🚫 Invading anyone's privacy (sharing personal info without consent)
• 🚫 Abusing members' family (parents, siblings, relatives)
• 🚫 Harassment, insults, or disrespect toward other players
• 🚫 Spam or off-topic spam

⚠️ <b>Violations:</b>
Repeated violations may result in removal from the group.

🤝 <b>Keep it Fun & Respectful!</b>
This group is for everyone. Let's play fair and treat each other with kindness.

Questions? Use /help for game commands!
    """
    await update.message.reply_text(group_description, parse_mode='HTML')

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Rate limiting
    if not check_rate_limit(user.id, 'profile'):
        return
    
    target_user_id = user.id
    target_username = user.first_name if user.first_name else "Player"
    
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied_user = update.message.reply_to_message.from_user
        target_user_id = replied_user.id
        target_username = replied_user.username if replied_user.username else (replied_user.first_name if replied_user.first_name else "Player")
    elif context.args and len(context.args) > 0:
        search_query = context.args[0].lstrip('@').lower().strip()
        
        try:
            if search_query.isdigit():
                target_user_id = int(search_query)
            else:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                
                # Try exact match first (case-insensitive)
                c.execute("SELECT user_id, username FROM leaderboard WHERE LOWER(TRIM(username)) = ? LIMIT 1", (search_query,))
                result = c.fetchone()
                
                # Try partial match (for partial names and nicknames)
                if not result:
                    c.execute("SELECT user_id, username FROM leaderboard WHERE LOWER(TRIM(username)) LIKE ? LIMIT 1", (f"%{search_query}%",))
                    result = c.fetchone()
                
                conn.close()
                
                if result:
                    target_user_id = result[0]
                    target_username = result[1]
                else:
                    await update.message.reply_text(f"❌ User '{context.args[0]}' not found!\n\n💡 Tips:\n• Make sure they've played at least one game\n• Reply to their message with /profile\n• Or use their numeric ID: /profile [user_id]")
                    return
        except Exception as e:
            logger.error(f"Profile search error: {e}")
            await update.message.reply_text(f"❌ Error searching for user!")
            return
    
    stats = db.get_player_stats(target_user_id)
    if not stats:
        await update.message.reply_text("❌ No stats found for this player!")
        return
    
    active_title = db.get_active_title(target_user_id)
    if target_user_id == BOT_OWNER_ID:
        active_title = 'kami'
    
    borders = {
        'kami': ('✨', '✨'),
        'legend': ('👑', '👑'),
        'warrior': ('⚔️', '⚔️'),
        'sage': ('🧙', '🧙'),
        'phoenix': ('🔥', '🔥'),
        'shadow': ('🌑', '🌑')
    }
    
    border_char = borders.get(active_title, ('•', '•'))[0]
    
    # Elaborate ornate borders
    profile_text = f"╔{'═' * 35}╗\n"
    profile_text += f"║ ✦ ✦ ✦  <b>👤 PLAYER PROFILE 👤</b>  ✦ ✦ ✦ ║\n"
    profile_text += f"║{' ' * 35}║\n"
    profile_text += f"║  {border_char} <b>─ ELITE PLAYER CARD ─</b> {border_char}     ║\n"
    profile_text += f"╚{'═' * 35}╝\n\n"
    
    # Player info section
    profile_text += f"  ▶ <b>USERNAME:</b> ‹ <b>{target_username}</b> ›\n"
    if active_title and active_title in TITLES:
        profile_text += f"  ▶ <b>TITLE:</b> ‹ {TITLES[active_title]['display']} ›\n\n"
    else:
        profile_text += f"  ▶ <b>TITLE:</b> ‹ 🔒 LOCKED 🔒 ›\n\n"
    
    # Ornate stats section
    profile_text += f"╔━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╗\n"
    profile_text += f"║ ◆  <b>📊 BATTLE STATISTICS 📊</b>  ◆ ║\n"
    profile_text += f"╠━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╣\n"
    profile_text += f"║  🎯 Total Score.........<b>{str(stats[7]).rjust(8)}</b>  ║\n"
    profile_text += f"║  📝 Words Played........<b>{str(stats[2]).rjust(8)}</b>  ║\n"
    profile_text += f"║  ⚡ Best Streak.........<b>{str(stats[6]).rjust(8)}</b>  ║\n"
    profile_text += f"║  🎮 Games Played........<b>{str(stats[3]).rjust(8)}</b>  ║\n"
    profile_text += f"║  📏 Longest Word........<b>{stats[4]} ({stats[5]} letters)</b>\n"
    profile_text += f"║  📈 Avg Word Length.....<b>{str(f'{stats[8]:.1f}').rjust(8)}</b>  ║\n"
    profile_text += f"╚━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╝\n\n"
    
    # Ornate achievements section
    profile_text += f"╔━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╗\n"
    profile_text += f"║ ★  <b>🏆 TROPHY COLLECTION 🏆</b>  ★ ║\n"
    profile_text += f"╠━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╣\n"
    
    unlocked = db.get_unlocked_titles(target_user_id)
    if target_user_id == BOT_OWNER_ID:
        unlocked.add('kami')
    
    if unlocked:
        for t in unlocked:
            if t in TITLES:
                profile_text += f"║  ⭐ {TITLES[t]['display']}\n"
    else:
        profile_text += "║  🔒 No achievements unlocked yet...\n"
    
    profile_text += f"╚━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╝\n"
    profile_text += f"\n  ✧ ✧ ✧  <b>END OF PROFILE</b>  ✧ ✧ ✧"
    
    try:
        profile_photos = await context.bot.get_user_profile_photos(target_user_id, limit=1)
        if profile_photos.photos:
            photo_list = profile_photos.photos[0]
            largest_photo = photo_list[-1]
            
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=largest_photo.file_id,
                caption=profile_text,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(profile_text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error fetching profile photo: {e}")
        await update.message.reply_text(profile_text, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """🎮 <b>COMPLETE GAMEPLAY GUIDE</b>

<b>🎯 GAME OBJECTIVE</b>
Submit valid English words that match the letter and length criteria each turn. Build streaks, earn points, and climb the leaderboard!

<b>📜 GAME RULES</b>
1. Words must start with the specified letter
2. Words must match the required length
3. Each word can only be used once per game
4. Invalid words result in an immediate turn skip
5. Time limit: 60 seconds per turn
6. Game eliminates players after timeout

<b>🎮 HOW TO PLAY</b>
/lobby → Open a game lobby
/join → Join an existing lobby
/difficulty [easy/medium/hard] → Set difficulty before /begin
/begin → Start the game (2+ players required)
• Type valid words during your turn
/stop → End the current game

<b>⚙️ DIFFICULTY MODES</b>
🟢 <b>Easy</b>: 3-10 letters, word length increases every 3 rounds
🟡 <b>Medium</b>: 3-15 letters, word length increases every 2 rounds
🔴 <b>Hard</b>: 4-20 letters, word length increases every round

<b>📊 SCORING SYSTEM</b>
• Points = word length (e.g., "hello" = 5 points)
• Build streaks for consecutive correct words
• 3+ streak = 🔥 STREAK bonus displayed
• Longest word ever = highest individual score
• Average word length tracked

<b>💰 SHOP & BOOSTS</b>
/shop → View available boosts and prices
/inventory → Check your current boosts

<b>Hint Boost (80 pts)</b>
/buy_hint → Purchase (3 suggestions, 2-min cooldown)

<b>Skip Boost (150 pts)</b>
/buy_skip → Purchase (skip turn penalty-free)

<b>Rebound Boost (250 pts)</b>
/buy_rebound → Purchase (skip & pass to next player)

/forfeit → Give up turn (-10 total points penalty)

<b>🏆 ACHIEVEMENTS & TITLES</b>
Unlock titles by meeting specific criteria:
👑 LEGEND: Reach 1000 total points
⚔️ WARRIOR: Achieve 10+ word streak
🧙 SAGE: Submit 50+ words
🔥 PHOENIX: Complete 10+ games
🌑 SHADOW: Find a 12+ letter word
✨ KAMI: Exclusive to bot owner

/achievements → View all titles
/settitle [title] → Equip a title
/progress → Check unlock requirements

<b>👤 PROFILES & STATS</b>
/mystats → View your statistics
/profile [@username] → View any player's profile with picture
/leaderboard [score/words/streak/longest] → Top 10 players

<b>📋 ALL COMMANDS</b>
<b>Setup:</b> /start /help /difficulty
<b>Game:</b> /lobby /join /begin /stop /forfeit
<b>Shop:</b> /shop /inventory /buy_hint /buy_skip /buy_rebound /hint /skip_boost /rebound
<b>Stats:</b> /mystats /profile /leaderboard /achievements /settitle /mytitle /progress

<b>💡 PRO TIPS</b>
✅ Start easy to learn mechanics
✅ Build long streaks for combo bonuses
✅ Longer words = more points
✅ Plan ahead for harder letters
✅ Use hints strategically when stuck
✅ Earn shop currency from word length
✅ Check /profile to see all player stats

🚀 <b>Ready to play? Type /lobby to get started!</b>"""
    await update.message.reply_text(help_text, parse_mode='HTML')

async def authority_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games:
        await update.message.reply_text(
            "❌ No lobby open! Steps:\n"
            "1. Type /lobby\n"
            "2. Then use /authority hint=2 skip=1 rebound=0",
            parse_mode='HTML'
        )
        return
    
    game = games[chat_id]
    
    if not game.is_lobby_open and not game.is_running:
        await update.message.reply_text(
            "❌ No active lobby! Type /lobby first.",
            parse_mode='HTML'
        )
        return
    
    if game.group_owner != user.id:
        await update.message.reply_text(
            f"❌ Only the lobby owner can use /authority!",
            parse_mode='HTML'
        )
        return
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "📋 <b>Usage:</b> /authority hint=X skip=Y rebound=Z\n\n"
            "<b>Example:</b> /authority hint=2 skip=1 rebound=0\n\n"
            "Sets max boosters per round. 0 = unlimited",
            parse_mode='HTML'
        )
        return
    
    try:
        updated = False
        for arg in context.args:
            if '=' not in arg:
                continue
            key, value = arg.split('=', 1)
            key = key.strip().lower()
            value_str = value.strip().lower()
            
            if key not in game.booster_limits:
                continue
            
            if value_str == 'null':
                game.booster_limits[key] = -1
                updated = True
            elif value_str.isdigit():
                value = int(value_str)
                if value == 0:
                    game.booster_limits[key] = float('inf')
                else:
                    game.booster_limits[key] = value
                updated = True
        
        if not updated:
            await update.message.reply_text(f"❌ Invalid format! Use: /authority hint=2 skip=1 rebound=null")
            return
        
        limits_text = ""
        for booster, limit in sorted(game.booster_limits.items()):
            if limit == -1:
                limits_text += f"  • {booster.capitalize()}: ❌ Disabled\n"
            elif limit == float('inf'):
                limits_text += f"  • {booster.capitalize()}: Unlimited\n"
            else:
                limits_text += f"  • {booster.capitalize()}: {int(limit)} max\n"
        
        await update.message.reply_text(
            f"✅ <b>Booster Limits Set!</b>\n\n{limits_text}",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Authority command error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error! Use: /authority hint=2 skip=1 rebound=0")

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

        if not game.is_practice:
            db.update_word_stats(user.id, user.first_name, word, current_streak)

        streak_bonus = ""
        if current_streak >= 3:
            streak_bonus = f"\n🔥 <b>{current_streak} STREAK!</b> You're on fire!"

        difficulty_increased = game.next_turn()
        
        msg_text = f"✅ '{word}' <b>(+{len(word)})</b>{streak_bonus}\n\n"
        
        if difficulty_increased:
            msg_text += f"📈 <b>DIFFICULTY INCREASED!</b> Now <b>{game.current_word_length}-letter</b> words!\n\n"
        
        next_player = game.players[game.current_player_index]
        turn_time = game.get_turn_time()
        game.current_turn_user_id = next_player['id']
        
        if game.is_practice:
            msg_text += f"💪 <b>Next Challenge:</b>\n"
            msg_text += f"Target: <b>{game.current_word_length}-letter</b> word starting with <b>'{game.current_start_letter.upper()}'</b>\n"
            msg_text += f"⏱️ <b>Time: {turn_time}s</b>"
        else:
            msg_text += f"👉 @{next_player['username']}'s Turn\n"
            msg_text += f"Target: <b>{game.current_word_length}-letter</b> word starting with <b>'{game.current_start_letter.upper()}'</b>\n"
            msg_text += f"⏱️ <b>Time: {turn_time}s</b>"

        await update.message.reply_text(msg_text, parse_mode='HTML')
        game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))
    except Exception as e:
        logger.error(f"Error processing word '{word}': {str(e)}", exc_info=True)
        await update.message.reply_text(f"❌ Error processing your word. Try again.")
        game.used_words.discard(word)


# ==========================================
# MAIN EXECUTION - PURE TELEGRAM BOT (runs in separate process via run.py)
# ==========================================
if __name__ == '__main__':
    if BOT_TOKEN == "REPLACE_WITH_TOKEN_IF_NOT_USING_SECRETS":
        print("ERROR: Please set up the BOT_TOKEN in Secrets or paste it in the code.")
    else:
        print("🎮 Telegram Bot Started", flush=True)
        
        # Infinite retry loop for bot
        retry_count = 0
        while True:
            try:
                print(f"🎮 Starting Telegram bot (attempt {retry_count + 1})...", flush=True)
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
                application.add_handler(CommandHandler("authority", authority_command))
                application.add_handler(CommandHandler("achievements", achievements_command))
                application.add_handler(CommandHandler("settitle", settitle_command))
                application.add_handler(CommandHandler("mytitle", mytitle_command))
                application.add_handler(CommandHandler("progress", progress_command))
                application.add_handler(CommandHandler("profile", profile_command))
                application.add_handler(CommandHandler("practice", practice_command))
                application.add_handler(CommandHandler("groupdesc", groupdesc_command))
                application.add_handler(CommandHandler("help", help_command))
                application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

                logger.info("Loaded dictionary words")
                print("🎮 BOT ONLINE - RUNNING FOREVER UNTIL MANUAL STOP!", flush=True)
                retry_count = 0
                application.run_polling()
            except KeyboardInterrupt:
                print("\n🛑 Bot stopped by user", flush=True)
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"Bot crash #{retry_count}: {str(e)}", exc_info=True)
                print(f"💥 Bot crashed: {e} | AUTO-RESTARTING IN 3s...", flush=True)
                time.sleep(3)