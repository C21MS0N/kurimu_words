import logging
import random
import sqlite3
import os
import asyncio
import time
import string
import sys
from typing import List, Dict, Set, Optional

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
BASE_TURN_TIME = 60  # Starting time for rounds

# Shop Boosts
SHOP_BOOSTS = {
    'hint': {'price': 80, 'description': '📖 Get dictionary meaning of a potential correct word'},
    'skip': {'price': 150, 'description': '⏭️ Skip your turn'},
    'rebound': {'price': 250, 'description': '🔄 Skip & pass same question to next player'}
}

# Bot Owner (for exclusive KAMI title) - Set via environment variable or hardcode here
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

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

        try:
            c.execute("ALTER TABLE inventory ADD COLUMN balance INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

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
                c.execute('UPDATE leaderboard SET total_score = ? WHERE user_id=?', (total_score, user_id))
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
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        entry = c.fetchone()
        if entry:
            new_games_played = entry[3] + 1
            c.execute("UPDATE leaderboard SET games_played = ? WHERE user_id=?", (new_games_played, user_id))
        else:
            c.execute("INSERT INTO leaderboard (user_id, games_played) VALUES (?, 1)", (user_id,))
        conn.commit()
        conn.close()

    def get_top_players(self, category='total_score', limit=10):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        valid_categories = ['total_score', 'total_words', 'longest_word_length', 'best_streak']
        if category not in valid_categories: category = 'total_score'
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
        self.dictionary_map: Dict[str, Set[str]] = {} # Optimizes challenge generation

        self.game_mode = 'nerd' # Default mode: 'chaos' or 'nerd'
        self.player_streaks: Dict[int, int] = {}
        self.eliminated_players: Set[int] = set()
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
        self.last_activity_time: float = time.time()

        self.load_dictionary()

    def load_dictionary(self):
        if os.path.exists(DICTIONARY_FILE):
            try:
                with open(DICTIONARY_FILE, 'r', encoding='utf-8') as f:
                    for line in f:
                        word = line.strip().lower()
                        if not word: continue
                        self.dictionary.add(word)
                        # Map for challenge generation
                        key = f"{len(word)}-{word[0]}"
                        if key not in self.dictionary_map:
                            self.dictionary_map[key] = set()
                        self.dictionary_map[key].add(word)
                logger.info(f"Loaded {len(self.dictionary)} words from {DICTIONARY_FILE}")
            except Exception as e:
                logger.error(f"Error loading dictionary: {e}")
                self.use_fallback_dictionary()
        else:
            logger.warning("Dictionary file not found. Using fallback list.")
            self.use_fallback_dictionary()

    def use_fallback_dictionary(self):
        words = [
            "cat", "dog", "bat", "rat", "hat", "mat", "sat", "pat",
            "bird", "word", "nerd", "curd", "herd", "blue", "glue",
            "apple", "board", "chair", "dance", "eagle", "fruit",
            "banana", "friend", "orange", "purple", "school",
            "elephant", "giraffe", "internet", "keyboard"
        ]
        self.dictionary = set(words)
        for word in words:
            key = f"{len(word)}-{word[0]}"
            if key not in self.dictionary_map:
                self.dictionary_map[key] = set()
            self.dictionary_map[key].add(word)

    def reset(self):
        self.is_running = False
        self.is_lobby_open = False
        self.players = []
        self.current_player_index = 0
        self.game_mode = 'nerd'
        self.current_word_length = 3
        self.used_words = set()
        self.turn_count = 0
        self.player_streaks = {}
        self.eliminated_players = set()
        self.turn_start_time = None
        self.group_owner = None
        self.booster_limits = {'hint': float('inf'), 'skip': float('inf'), 'rebound': float('inf')}
        self.booster_usage = {'hint': 0, 'skip': 0, 'rebound': 0}
        if self.timeout_task:
            self.timeout_task.cancel()
            self.timeout_task = None

    def set_mode(self, mode: str):
        if mode in ['chaos', 'nerd']:
            self.game_mode = mode
            if mode == 'nerd':
                self.current_word_length = 3
            return True
        return False

    def generate_valid_challenge(self, mode: str):
        """Generates a random letter and length that definitely exists in the dictionary"""
        attempts = 0
        while attempts < 200:
            if mode == 'chaos':
                # Random length (3-12), Random letter
                target_len = random.randint(3, 12)
                target_letter = random.choice(string.ascii_lowercase)
            else: 
                # Nerd mode: fixed length passed in, check if letter exists
                target_len = self.current_word_length
                target_letter = random.choice(string.ascii_lowercase)

            key = f"{target_len}-{target_letter}"

            # Check if this combination exists in our map and has unused words
            if key in self.dictionary_map:
                available_words = self.dictionary_map[key]
                if len(available_words - self.used_words) > 0:
                    return target_len, target_letter

            attempts += 1

        # Fallback if random gen fails (should rarely happen with good dict)
        return 3, 'a'

    def next_turn(self):
        self.current_player_index = (self.current_player_index + 1) % len(self.players)
        self.turn_count += 1

        num_players = len(self.players) if self.players else 1

        # Difficulty Progression - Nerd Mode
        if self.game_mode == 'nerd':
            # Length increases every round (when every player has played once)
            if self.turn_count % num_players == 0:
                self.current_word_length += 1
                if self.current_word_length > 15: # Soft cap
                    self.current_word_length = 15

        # Generate challenge based on mode
        if self.rebound_target_letter:
            self.current_word_length = self.rebound_target_length
            self.current_start_letter = self.rebound_target_letter
            self.rebound_target_letter = None
        else:
            self.current_word_length, self.current_start_letter = self.generate_valid_challenge(self.game_mode)

        self.turn_start_time = time.time()
        return True

    def get_turn_time(self) -> int:
        # Time cuts by 5s every round
        num_players = len(self.players) if self.players else 1
        rounds_completed = self.turn_count // num_players
        time_reduction = rounds_completed * 5

        # Minimum 5 seconds
        return max(5, BASE_TURN_TIME - time_reduction)

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

    def initialize_player_stats(self, user_id: int):
        if user_id not in self.player_streaks:
            self.player_streaks[user_id] = 0

# Key: chat_id, Value: GameState
games: Dict[int, GameState] = {}
db = DatabaseManager(DB_FILE)

# ==========================================
# UTILS & CLEANUP
# ==========================================
BOT_START_TIME = time.time()
STALE_MESSAGE_THRESHOLD = 5
user_command_cooldowns: Dict[int, Dict[str, float]] = {}
COMMAND_COOLDOWN_SECONDS = 1
GAME_CLEANUP_INTERVAL = 3600

def is_message_stale(update: Update) -> bool:
    if not update.message or not update.message.date:
        return False
    message_timestamp = update.message.date.timestamp()
    current_time = time.time()
    if current_time - message_timestamp > STALE_MESSAGE_THRESHOLD:
        return True
    return False

def check_rate_limit(user_id: int, command: str) -> bool:
    current_time = time.time()
    if user_id not in user_command_cooldowns:
        user_command_cooldowns[user_id] = {}
    if command in user_command_cooldowns[user_id]:
        last_use = user_command_cooldowns[user_id][command]
        if current_time - last_use < COMMAND_COOLDOWN_SECONDS:
            return False
    user_command_cooldowns[user_id][command] = current_time
    return True

async def cleanup_old_games():
    while True:
        try:
            await asyncio.sleep(GAME_CLEANUP_INTERVAL)
            current_time = time.time()
            to_delete = []
            for chat_id, game in games.items():
                if not game.is_running and not game.is_lobby_open:
                    if current_time - game.last_activity_time > GAME_CLEANUP_INTERVAL:
                        to_delete.append(chat_id)
            for cid in to_delete:
                del games[cid]
        except Exception:
            pass

async def handle_turn_timeout(chat_id: int, user_id: int, application):
    try:
        if chat_id not in games: return
        game = games[chat_id]

        # Wait for turn duration
        await asyncio.sleep(game.get_turn_time())

        if chat_id not in games or not games[chat_id].is_running:
            return

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
                text=f"⏰ <b>TIME'S UP!</b>\n❌ You were eliminated!\n(Practice mode)",
                parse_mode='HTML'
            )
        else:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ <b>TIME'S UP!</b>\n❌ @{current_player['username']} is eliminated due to timeout!",
                parse_mode='HTML'
            )

        game.next_turn()

        # Check Win Condition
        if len(game.eliminated_players) >= len(game.players) - 1:
            winner = next((p for p in game.players if p['id'] not in game.eliminated_players), None)
            if winner:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"🏆 *GAME OVER\\!*\n👑 *Winner:* @{winner['username']}",
                    parse_mode='MarkdownV2'
                )
                for player in game.players:
                    db.increment_games_played(player['id'])
            game.reset()
            return

        # Find next valid player
        next_player = game.players[game.current_player_index]
        max_iterations = len(game.players)
        iterations = 0
        while next_player['id'] in game.eliminated_players and iterations < max_iterations:
            game.next_turn()
            next_player = game.players[game.current_player_index]
            iterations += 1

        if next_player['id'] in game.eliminated_players:
            game.reset()
            return

        turn_time = game.get_turn_time()
        game.current_turn_user_id = next_player['id']

        await application.bot.send_message(
            chat_id=chat_id,
            text=f"👉 @{next_player['username']}'s Turn\n"
                 f"Target: *exactly {game.current_word_length} letters* starting with *{game.current_start_letter.upper()}*\n"
                 f"⏱️ *Time: {turn_time}s*",
            parse_mode='MarkdownV2'
        )

        game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], application))
    except Exception as e:
        logger.error(f"Timeout handler error: {e}")

# ==========================================
# BOT COMMANDS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update): return
    user = update.effective_user
    if BOT_OWNER_ID > 0 and user.id == BOT_OWNER_ID:
        db.unlock_title(user.id, 'kami')

    await update.message.reply_text(
        "🎮 <b>Welcome to the Infinite Word Game!</b>\n\n"
        "📋 <b>Game Commands:</b>\n"
        "/lobby - Open a new game lobby\n"
        "/join - Join the lobby\n"
        "/mode [chaos/nerd] - Set game mode\n"
        "/begin - Start the game (needs 2+ players)\n"
        "/forfeit - Give up your turn\n"
        "/stop - Stop the current game\n\n"
        "🔥 <b>Game Modes:</b>\n"
        "🎲 <b>Chaos:</b> Random letters & lengths. Time cuts every round.\n"
        "🤓 <b>Nerd:</b> Length increases & time cuts every round.\n\n"
        "💰 <b>Shop & Boosts:</b>\n"
        "/shop - View available boosts\n"
        "/buy_hint /buy_skip /buy_rebound - Purchase boosts\n"
        "/inventory - See items\n\n"
        "📊 <b>Stats:</b>\n"
        "/mystats, /leaderboard, /achievements, /profile\n\n"
        "🔐 <b>Admin:</b>\n"
        "/omnipotent [points] - Gift points (reply to user)",
        parse_mode='HTML'
    )

async def lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update): return
    chat_id = update.effective_chat.id
    if chat_id not in games:
        games[chat_id] = GameState(chat_id=chat_id, application=context.application)
    game = games[chat_id]

    if game.is_running:
        await update.message.reply_text("⚠️ Game in progress! Finish it or type /stop.")
        return
    if game.is_lobby_open:
        await update.message.reply_text(f"✅ Lobby open! Mode: <b>{game.game_mode.upper()}</b> | Type /join to enter.", parse_mode='HTML')
        return

    game.reset()
    game.is_lobby_open = True
    game.group_owner = update.effective_user.id

    user = update.effective_user
    display_name = str(user.first_name or user.username or "Player").strip()
    if display_name == "None" or not display_name: display_name = "Player"
    username_to_store = (user.username if user.username else display_name).lstrip('@')

    game.players.append({'id': user.id, 'name': display_name, 'username': username_to_store})
    db.ensure_player_exists(user.id, username_to_store)

    await update.message.reply_text(
        f"📢 <b>Lobby Opened!</b>\n"
        f"Mode: <b>{game.game_mode.upper()}</b> | Change with /mode\n\n"
        f"{display_name} joined. Waiting for others...\n"
        f"Type /join to play!",
        parse_mode='HTML'
    )

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update): return
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
    if display_name == "None" or not display_name: display_name = "Player"
    username_to_store = (user.username if user.username else display_name).lstrip('@')

    game.players.append({'id': user.id, 'name': display_name, 'username': username_to_store})
    game.initialize_player_stats(user.id)
    db.ensure_player_exists(user.id, username_to_store)
    await update.message.reply_text(f"✅ {display_name} joined! (Total: {len(game.players)})", parse_mode='HTML')

async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games:
        games[chat_id] = GameState(chat_id=chat_id, application=context.application)
    game = games[chat_id]

    if game.is_running:
        await update.message.reply_text("❌ Cannot change mode during an active game!")
        return

    if not context.args:
        await update.message.reply_text(
            f"🎯 Current Mode: <b>{game.game_mode.upper()}</b>\n\n"
            "🎲 <b>CHAOS</b>: Random letters & lengths. Time decreases per round.\n"
            "🤓 <b>NERD</b>: Starts at 3 letters. Length increases & time decreases per round.\n\n"
            "Use: /mode [chaos/nerd]",
            parse_mode='HTML'
        )
        return

    new_mode = context.args[0].lower()
    if game.set_mode(new_mode):
        await update.message.reply_text(f"✅ Game mode set to <b>{new_mode.upper()}</b>!", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Invalid mode! Use: chaos or nerd")

async def begin_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_stale(update): return
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

    # Initialize First Turn
    if game.game_mode == 'nerd':
        game.current_word_length = 3

    game.current_word_length, game.current_start_letter = game.generate_valid_challenge(game.game_mode)

    game.turn_start_time = time.time()
    current_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = current_player['id']

    player_names = ', '.join([str(p['name']) for p in game.players])
    mode_desc = "Length increases every round!" if game.game_mode == 'nerd' else "Random chaos!"

    await update.message.reply_text(
        f"🎮 *Game Started\\!*\n"
        f"Mode: *{game.game_mode.upper()}* \\({mode_desc}\\)\n"
        f"Players: {player_names}\n\n"
        f"👉 {str(current_player['name'])}'s turn\\!\n"
        f"Target: *exactly {game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n"
        f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )

    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, current_player['id'], context.application))

async def stop_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id not in games: return
    game = games[chat_id]

    is_lobby_creator = user.id == game.group_owner
    is_admin = False
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ['administrator', 'creator']
    except: pass

    if not is_lobby_creator and not is_admin:
        await update.message.reply_text("❌ Only the lobby creator or admins can stop the game!")
        return

    game.reset()
    await update.message.reply_text("🛑 Game stopped.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category_map = {'score': 'total_score', 'words': 'total_words', 'streak': 'best_streak', 'longest': 'longest_word_length'}
    category_input = context.args[0].lower() if context.args else 'score'
    category = category_map.get(category_input, 'total_score')

    top = db.get_top_players(category=category, limit=10)
    if not top:
        await update.message.reply_text("🏆 Leaderboard is empty!")
        return

    text = f"🏆 <b>Leaderboard</b> 🏆\n\n"
    for idx, (name, value) in enumerate(top, 1):
        emoji = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
        text += f"{emoji} <b>{name}</b> - {value}\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def forfeit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games or not games[chat_id].is_running: return
    game = games[chat_id]
    user = update.effective_user
    current_player = game.players[game.current_player_index]

    if user.id != current_player['id']: return

    game.cancel_timeout()
    game.eliminated_players.add(user.id)
    game.reset_streak(user.id)
    db.update_word_stats(user.id, user.first_name, "", 0, forfeit=True)

    await update.message.reply_text(f"⛔ <b>Forfeit!</b> (-10 pts)", parse_mode='HTML')
    game.next_turn()

    if len(game.eliminated_players) >= len(game.players) - 1:
        winner = next((p for p in game.players if p['id'] not in game.eliminated_players), None)
        if winner:
            await update.message.reply_text(f"🏆 *Winner:* @{winner['username']}", parse_mode='MarkdownV2')
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
        f"Target: *exactly {game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n"
        f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))

# ==========================================
# SHOP & STATS & HELP COMMANDS
# ==========================================
async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_rate_limit(user.id, 'shop'): return
    balance = db.get_balance(user.id)
    inventory = db.get_inventory(user.id)

    text = f"🛍️ <b>SHOP</b> 💰 Balance: <b>{balance} pts</b>\n\n"
    for boost_type, details in SHOP_BOOSTS.items():
        text += f"{details['description']}\n💵 Price: <b>{details['price']}</b> - Owned: <b>{inventory[boost_type]}</b>\n/buy_{boost_type}\n\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def buy_boost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_rate_limit(user.id, 'buy'): return
    message_text = update.message.text.lower()
    boost_type = next((b for b in SHOP_BOOSTS if f"/buy_{b}" in message_text), None)

    if boost_type:
        price = SHOP_BOOSTS[boost_type]['price']
        if db.buy_boost(user.id, boost_type, price):
            await update.message.reply_text(f"✅ Purchased {boost_type}!")
        else:
            await update.message.reply_text(f"❌ Insufficient points.")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_rate_limit(user.id, 'mystats'): return
    stats = db.get_player_stats(user.id)
    if not stats:
        await update.message.reply_text("📊 No stats yet!")
        return
    text = f"📊 <b>{user.first_name}'s Stats</b>\nTotal Score: {stats[7]}\nWords: {stats[2]}\nBest Streak: {stats[6]}"
    await update.message.reply_text(text, parse_mode='HTML')

async def omnipotent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if update.effective_chat.type == 'private': return

    is_admin = False
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ['creator', 'administrator']
    except: pass

    if not is_admin: return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a user with /omnipotent [points]")
        return

    target_user = update.message.reply_to_message.from_user
    points = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
    if points > 0:
        db.add_balance(target_user.id, points)
        await update.message.reply_text(f"✨ Gifted {points} pts to @{target_user.username}!")

async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    newly_unlocked = db.auto_unlock_titles(user.id)
    if newly_unlocked:
        await update.message.reply_text(f"🎉 New titles unlocked: {', '.join(newly_unlocked)}")

    unlocked = db.get_unlocked_titles(user.id)
    text = "🏆 <b>Achievements</b>\n\n"
    for k, v in TITLES.items():
        status = "✅" if k in unlocked else "🔒"
        text += f"{status} <b>{v['display']}</b>\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db.get_player_stats(user.id)
    if not stats:
        await update.message.reply_text("No profile found.")
        return
    title = db.get_active_title(user.id)
    display_title = TITLES[title]['display'] if title in TITLES else "None"

    text = f"👤 <b>{user.first_name}</b>\nTitle: {display_title}\nScore: {stats[7]}\nWords: {stats[2]}"
    await update.message.reply_text(text, parse_mode='HTML')

async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    inv = db.get_inventory(user.id)
    await update.message.reply_text(f"🎒 Hints: {inv['hint']} | Skips: {inv['skip']} | Rebounds: {inv['rebound']}")

async def authority_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id not in games: return
    game = games[chat_id]
    if game.group_owner != user.id:
        await update.message.reply_text("❌ Only lobby owner.")
        return
    await update.message.reply_text("✅ Authority updated (Mock).")

async def practice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in games: return
    games[chat_id] = GameState(chat_id=chat_id, application=context.application)
    game = games[chat_id]
    game.is_practice = True
    game.is_running = True
    game.players = [{'id': update.effective_user.id, 'name': update.effective_user.first_name, 'username': update.effective_user.username}]
    game.current_word_length, game.current_start_letter = game.generate_valid_challenge('nerd')
    game.current_turn_user_id = update.effective_user.id
    await update.message.reply_text(f"Practice started! {game.current_word_length} letters, start {game.current_start_letter}")
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, update.effective_user.id, context.application))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """🎮 <b>GAMEPLAY GUIDE</b>

<b>🎯 HOW TO PLAY</b>
Submit valid English words matching the Letter and Length.
/lobby → Open lobby
/join → Join game
/mode [chaos/nerd] → Select mode
/begin → Start

<b>🔥 GAME MODES</b>
🎲 <b>CHAOS:</b> Random letters & random lengths (3-12). Time reduces every round.
🤓 <b>NERD:</b> Starts at 3 letters. Length increases +1 every round. Time reduces every round.

<b>🕵️ SECRET & ADMIN COMMANDS</b>
/omnipotent [points] → (Admin Only) Reply to a user to gift them shop points.
/authority hint=2 → (Admin Only) Limit boosters per game.

<b>💰 SHOP</b>
/shop → Buy hints/skips
/buy_hint, /buy_skip, /buy_rebound

<b>📊 STATS</b>
/mystats, /leaderboard, /achievements, /profile"""
    await update.message.reply_text(help_text, parse_mode='HTML')

# ==========================================
# MESSAGE HANDLER
# ==========================================
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
        await update.message.reply_text(f"❌ Length must be {game.current_word_length}!")
        return
    if not word.startswith(game.current_start_letter):
        await update.message.reply_text(f"❌ Must start with '{game.current_start_letter.upper()}'!")
        return
    if word in game.used_words:
        await update.message.reply_text("❌ Already used!")
        return
    if word not in game.dictionary:
        await update.message.reply_text("❌ Not in dictionary!")
        return

    game.cancel_timeout()
    game.used_words.add(word)
    game.increment_streak(user.id)

    if not game.is_practice:
        db.update_word_stats(user.id, user.first_name, word, game.get_streak(user.id))

    game.next_turn()

    next_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']

    msg_text = f"✅ '{word}' accepted! (+{len(word)} pts)\n\n"
    msg_text += f"👉 @{next_player['username']}'s Turn\n"
    msg_text += f"Target: <b>exactly {game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>\n"
    msg_text += f"⏱️ <b>Time: {turn_time}s</b>"

    await update.message.reply_text(msg_text, parse_mode='HTML')
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == '__main__':
    if BOT_TOKEN == "REPLACE_WITH_TOKEN_IF_NOT_USING_SECRETS":
        print("ERROR: Please set up the BOT_TOKEN.")
    else:
        print("🎮 Telegram Bot Started", flush=True)
        try:
            application = ApplicationBuilder().token(BOT_TOKEN).build()

            # Commands
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("lobby", lobby))
            application.add_handler(CommandHandler("join", join))
            application.add_handler(CommandHandler("mode", mode_command))
            application.add_handler(CommandHandler("begin", begin_game))
            application.add_handler(CommandHandler("stop", stop_game))
            application.add_handler(CommandHandler("forfeit", forfeit_command))
            application.add_handler(CommandHandler("leaderboard", leaderboard))
            application.add_handler(CommandHandler("shop", shop_command))
            application.add_handler(CommandHandler("buy_hint", buy_boost_command))
            application.add_handler(CommandHandler("buy_skip", buy_boost_command))
            application.add_handler(CommandHandler("buy_rebound", buy_boost_command))
            application.add_handler(CommandHandler("mystats", mystats_command))
            application.add_handler(CommandHandler("omnipotent", omnipotent_command))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("inventory", inventory_command))
            application.add_handler(CommandHandler("achievements", achievements_command))
            application.add_handler(CommandHandler("profile", profile_command))
            application.add_handler(CommandHandler("authority", authority_command))
            application.add_handler(CommandHandler("practice", practice_command))

            application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

            application.run_polling()
        except Exception as e:
            logger.error(f"Bot crash: {str(e)}")