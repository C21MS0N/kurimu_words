import logging
import string
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
    'rebound': {'price': 250, 'description': '🔄 Skip & pass same question to next player'},
    'streak': {'price': 400, 'description': '🛡️ Streak Protection - Prevent next streak reset'},
    'bal_photo': {'price': 1500, 'description': '🖼️ Custom /bal Picture - Set your own balance photo'}
}

# Game Challenge Sequence (length, letter) - cycles through
CHALLENGE_SEQUENCE = [
    (4, 'n'),   # 4+ letters starting with N
    (6, 'c'),   # 6+ letters starting with C
    (5, 's'),   # 5+ letters starting with S
    (3, 'd'),   # 3+ letters starting with D
    (7, 'p'),   # 7+ letters starting with P
    (4, 'a'),   # 4+ letters starting with A
]

# Bot Owner (for exclusive KAMI title) - Set via environment variable or hardcode here
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))  # Set BOT_OWNER_ID env var to your Telegram user ID

# Available Titles with Dynamic Requirements (Multi-Stage)
STAGES = {
    1: {'display': 'Ⅰ', 'color': '⚪', 'multiplier': 3},
    2: {'display': 'Ⅱ', 'color': '🟢', 'multiplier': 6},
    3: {'display': 'Ⅲ', 'color': '🔵', 'multiplier': 9},
    4: {'display': 'Ⅳ', 'color': '🟡', 'multiplier': 12},
    5: {'display': 'Ⅴ', 'color': '💎', 'multiplier': 15},
}

TITLES = {
    'legend': {'display': '👑 LEGEND', 'base_req': 1000, 'stat': 'total_score', 'desc': 'Reach {req} total points'},
    'warrior': {'display': '⚔️ WARRIOR', 'base_req': 5, 'stat': 'best_streak', 'desc': 'Achieve {req}+ word streak'},
    'sage': {'display': '🧙 SAGE', 'base_req': 50, 'stat': 'total_words', 'desc': 'Submit {req}+ words'},
    'phoenix': {'display': '🔥 PHOENIX', 'base_req': 10, 'stat': 'games_played', 'desc': 'Complete {req}+ games'},
    'shadow': {'display': '🌑 SHADOW', 'base_req': 1, 'stat': 'longest_word_length', 'desc': 'Find a {req}+ letter word'},
    'kami': {'display': '✨ KAMI', 'exclusive': True}
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
                streak_protect INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                bal_photo_count INTEGER DEFAULT 0
            )
        ''')
        
        # Migration for existing inventory table
        try:
            c.execute("ALTER TABLE inventory ADD COLUMN bal_photo_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Force update NULLs to 0 to prevent "None" errors
        try:
            c.execute("UPDATE inventory SET bal_photo_count = 0 WHERE bal_photo_count IS NULL")
            c.execute("UPDATE inventory SET hint_count = 0 WHERE hint_count IS NULL")
            c.execute("UPDATE inventory SET skip_count = 0 WHERE skip_count IS NULL")
            c.execute("UPDATE inventory SET rebound_count = 0 WHERE rebound_count IS NULL")
            c.execute("UPDATE inventory SET streak_protect = 0 WHERE streak_protect IS NULL")
            c.execute("UPDATE inventory SET balance = 0 WHERE balance IS NULL")
        except sqlite3.OperationalError:
            pass

        try:
            c.execute("ALTER TABLE leaderboard ADD COLUMN last_daily TEXT")
        except sqlite3.OperationalError:
            pass
        
        # Create titles table
        c.execute('''
            CREATE TABLE IF NOT EXISTS titles (
                user_id INTEGER PRIMARY KEY,
                active_title TEXT DEFAULT '',
                unlocked_titles TEXT DEFAULT '',
                bio TEXT DEFAULT '',
                has_bio_access INTEGER DEFAULT 0,
                custom_bal_photo_id TEXT DEFAULT '',
                has_bal_photo_access INTEGER DEFAULT 0
            )
        ''')

        # Migration for existing titles table
        try:
            c.execute("ALTER TABLE titles ADD COLUMN custom_bal_photo_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE titles ADD COLUMN has_bal_photo_access INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Create chat_members table
        c.execute('''
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        
        # Create permissions table
        c.execute('''
            CREATE TABLE IF NOT EXISTS permissions (
                user_id INTEGER PRIMARY KEY,
                is_omnipotent INTEGER DEFAULT 0
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
        # Format: "title:stage,title:stage"
        return result[0].split(',') if result and result[0] else []
    
    def get_title_stage(self, user_id, title_key):
        unlocked = self.get_unlocked_titles(user_id)
        for entry in unlocked:
            if ':' in entry:
                k, s = entry.split(':')
                if k == title_key:
                    return int(s)
        return 0

    def unlock_title_stage(self, user_id, title_key, stage):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        unlocked = self.get_unlocked_titles(user_id)
        
        # Update or add
        new_unlocked = []
        found = False
        for entry in unlocked:
            if ':' in entry:
                k, s = entry.split(':')
                if k == title_key:
                    new_unlocked.append(f"{title_key}:{stage}")
                    found = True
                else:
                    new_unlocked.append(entry)
            else:
                new_unlocked.append(entry)
        
        if not found:
            new_unlocked.append(f"{title_key}:{stage}")
            
        c.execute("UPDATE titles SET unlocked_titles = ? WHERE user_id=?", (','.join(new_unlocked), user_id))
        conn.commit()
        conn.close()

    def check_title_unlock(self, user_id, title_key, stage=1):
        if title_key == 'kami':
            return user_id == BOT_OWNER_ID
        
        if title_key not in TITLES:
            return False
            
        stats = self.get_player_stats(user_id)
        if not stats:
            return False
            
        title_data = TITLES[title_key]
        req_val = int(title_data['base_req'] * STAGES[stage]['multiplier'])
        
        # stats mapping: 2: total_words, 3: games_played, 5: longest_word_length, 6: best_streak, 7: total_score
        stat_map = {
            'total_words': stats[2],
            'games_played': stats[3],
            'longest_word_length': stats[5],
            'best_streak': stats[6],
            'total_score': stats[7]
        }
        
        # Shadow Special Logic: Strict 3/6/9/12/15 word length
        if title_key == 'shadow':
            shadow_reqs = {1: 3, 2: 6, 3: 9, 4: 12, 5: 15}
            return stat_map['longest_word_length'] >= shadow_reqs.get(stage, 15)
            
        return stat_map.get(title_data['stat'], 0) >= req_val
    
    def auto_unlock_titles(self, user_id):
        newly_unlocked = []
        for title_key, title_data in TITLES.items():
            if title_key == 'kami': continue
            
            current_stage = self.get_title_stage(user_id, title_key)
            for stage in range(current_stage + 1, 6):
                if self.check_title_unlock(user_id, title_key, stage):
                    self.unlock_title_stage(user_id, title_key, stage)
                    newly_unlocked.append((title_key, stage))
                else:
                    break
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

    def is_user_omnipotent(self, user_id):
        """Check if user has omnipotent permissions"""
        if user_id == BOT_OWNER_ID: return True
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT is_omnipotent FROM permissions WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] == 1 if result else False

    def set_user_omnipotent(self, user_id, status: bool):
        """Grant or revoke omnipotent permissions"""
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO permissions (user_id, is_omnipotent) VALUES (?, ?)", 
                 (user_id, 1 if status else 0))
        conn.commit()
        conn.close()

    def add_balance(self, user_id, amount):
        """Add points to user's shop balance (Currency only)"""
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        
        # Update inventory balance
        c.execute("SELECT * FROM inventory WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO inventory (user_id, balance) VALUES (?, ?)", (user_id, amount))
        else:
            c.execute("UPDATE inventory SET balance = balance + ? WHERE user_id=?", (amount, user_id))
            
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
    
    def get_player_last_daily(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT last_daily FROM leaderboard WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def update_player_last_daily(self, user_id, date_str):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("UPDATE leaderboard SET last_daily = ? WHERE user_id = ?", (date_str, user_id))
        conn.commit()
        conn.close()

    def get_inventory(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT hint_count, skip_count, rebound_count, balance, streak_protect, bal_photo_count FROM inventory WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        if result: 
            return {
                'hint': result[0] or 0, 
                'skip': result[1] or 0, 
                'rebound': result[2] or 0,
                'balance': result[3] or 0,
                'streak': result[4] or 0,
                'streak_protect': result[4] or 0,
                'bal_photo': result[5] or 0
            }
        return {'hint': 0, 'skip': 0, 'rebound': 0, 'streak': 0, 'streak_protect': 0, 'bal_photo': 0, 'balance': 0}
    
    def buy_boost(self, user_id, boost_type, price):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        
        # Check balance
        c.execute("SELECT balance FROM inventory WHERE user_id=?", (user_id,))
        res = c.fetchone()
        if not res or res[0] < price:
            conn.close()
            return False
            
        # Deduct balance
        c.execute("UPDATE inventory SET balance = balance - ? WHERE user_id=?", (price, user_id))
        
        # Add to inventory (except for one-time access items like bio and bal_photo)
        if boost_type not in ['bio', 'bal_photo']:
            col_map = {
                'hint': 'hint_count',
                'skip': 'skip_count',
                'rebound': 'rebound_count',
                'streak': 'streak_protect'
            }
            col = col_map.get(boost_type)
            if col:
                c.execute(f"UPDATE inventory SET {col} = {col} + 1 WHERE user_id=?", (user_id,))
        elif boost_type == 'bio':
            # Ensure titles record exists
            c.execute("SELECT user_id FROM titles WHERE user_id=?", (user_id,))
            if not c.fetchone():
                c.execute("INSERT INTO titles (user_id, has_bio_access) VALUES (?, 1)", (user_id,))
            else:
                c.execute("UPDATE titles SET has_bio_access = 1 WHERE user_id=?", (user_id,))
        elif boost_type == 'bal_photo':
            # Fix: Ensure record exists and update license
            c.execute("SELECT user_id FROM titles WHERE user_id=?", (user_id,))
            if not c.fetchone():
                c.execute("INSERT INTO titles (user_id, has_bal_photo_access) VALUES (?, 1)", (user_id,))
            else:
                c.execute("UPDATE titles SET has_bal_photo_access = 1 WHERE user_id=?", (user_id,))
        
        conn.commit()
        conn.close()
        return True
    
    def use_boost(self, user_id, boost_type):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        
        col_map = {
            'hint': 'hint_count',
            'skip': 'skip_count',
            'rebound': 'rebound_count',
            'streak_protect': 'streak_protect'
        }
        col = col_map.get(boost_type, f"{boost_type}_count")
        
        c.execute(f"UPDATE inventory SET {col} = {col} - 1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
    
    def get_custom_bal_photo(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT custom_bal_photo_id FROM titles WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result and result[0] else None

    def set_custom_bal_photo(self, user_id, file_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("UPDATE titles SET custom_bal_photo_id = ?, has_bal_photo_access = 0 WHERE user_id=?", (file_id, user_id))
        if c.rowcount == 0:
            c.execute("INSERT INTO titles (user_id, custom_bal_photo_id, has_bal_photo_access) VALUES (?, ?, 0)", (user_id, file_id))
        conn.commit()
        conn.close()

    def has_bal_photo_access(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT has_bal_photo_access FROM titles WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 0

    def get_bio(self, user_id):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT bio, has_bio_access FROM titles WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result if result else (None, 0)

    def set_bio(self, user_id, bio_text):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("UPDATE titles SET bio = ?, has_bio_access = 0 WHERE user_id=?", (bio_text, user_id))
        if c.rowcount == 0:
            c.execute("INSERT INTO titles (user_id, bio, has_bio_access) VALUES (?, ?, 0)", (user_id, bio_text))
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
        self.is_cpu_game: bool = False
        self.cpu_difficulty: str = 'medium'
        self.game_mode: str = 'nerd'  # 'chaos' or 'nerd'
        self.last_activity_time: float = time.time()  # Track for memory cleanup
        self.challenge_index: int = 0  # Track position in challenge sequence

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
        self.current_start_letter = random.choice(string.ascii_lowercase) # Random start
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
            # Don't reset word length if game is already running
            if not self.is_running:
                self.current_word_length = config['start_length']
            return True
        return False

    def next_turn(self, preserve_challenge=False):
        self.current_player_index = (self.current_player_index + 1) % len(self.players)
        self.turn_count += 1

        if not preserve_challenge:
            # Randomize challenges based on mode
            self.current_start_letter = random.choice(string.ascii_lowercase)
            
            if self.game_mode == 'chaos':
                # Chaos: random length (3-12)
                self.current_word_length = random.randint(3, 12)
            else:
                # Nerd: progressive length
                # Starts at 3, increases every round (all players have one turn)
                num_players = len(self.players) if self.players else 1
                rounds_completed = self.turn_count // num_players
                self.current_word_length = min(3 + rounds_completed, 15)

        difficulty_increased = self.turn_count % 6 == 0
        if difficulty_increased:
            self.difficulty_level += 1

        self.turn_start_time = time.time()
        self.last_word_length = self.current_word_length
        return difficulty_increased
    
    def get_turn_time(self) -> int:
        base_time = 30
        time_reduction = self.difficulty_level * 5
        return max(5, base_time - time_reduction)
    
    def cancel_timeout(self):
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
            self.timeout_task = None

    def get_streak(self, user_id: int) -> int:
        return self.player_streaks.get(user_id, 0)

    def increment_streak(self, user_id: int):
        self.player_streaks[user_id] = self.player_streaks.get(user_id, 0) + 1

    def reset_streak(self, user_id: int, is_timeout: bool = False):
        if user_id in self.player_streaks:
            # If they have protection, use it and don't reset
            inventory = db.get_inventory(user_id)
            if inventory.get('streak_protect', 0) > 0:
                db.use_boost(user_id, 'streak_protect')
                return
            
            # Reset to 0 if no protection
            self.player_streaks[user_id] = 0

    # Removed can_use_hint, use_hint, can_skip, use_skip, get_hint_words methods

    def initialize_player_stats(self, user_id: int):
        if user_id not in self.player_streaks:
            self.player_streaks[user_id] = 0
    
    def get_cpu_word(self) -> Optional[str]:
        """AI selects a word for CPU turn based on difficulty"""
        valid_words = [w for w in self.dictionary if len(w) == self.current_word_length and w.startswith(self.current_start_letter) and w not in self.used_words]
        if not valid_words:
            return None
        
        if self.cpu_difficulty == 'easy':
            # Easy: random word (occasional mistakes)
            if random.random() < 0.25:
                return random.choice(valid_words)
        elif self.cpu_difficulty == 'hard':
            # Hard: always pick longest word
            return max(valid_words, key=len)
        
        # Medium: smart random selection
        return random.choice(valid_words)

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
    """Handle turn timeout - eliminate player"""
    try:
        # Get turn time from game state or default
        if chat_id not in games: return
        turn_time = games[chat_id].get_turn_time()
            
        await asyncio.sleep(turn_time)
        
        if chat_id not in games: return
        game = games[chat_id]
        
        # Check if it's still this user's turn
        current_player = game.players[game.current_player_index]
        if not game.is_running or current_player['id'] != user_id:
            return

        # Player timed out
        game.eliminated_players.add(user_id)
        game.reset_streak(user_id, is_timeout=True)
        
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
        
        # Check for winner
        if len(game.eliminated_players) >= len(game.players) - 1:
            winner = next((p for p in game.players if p['id'] not in game.eliminated_players), None)
            if winner:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"🏆 <b>GAME OVER!</b>\n\n👑 <b>Winner:</b> @{winner['username']}",
                    parse_mode='HTML'
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
            for player in game.players:
                db.increment_games_played(player['id'])
            game.reset()
            await application.bot.send_message(chat_id, "❌ No valid players remaining. Game reset.")
            return
        
        turn_time = game.get_turn_time()
        game.current_turn_user_id = next_player['id']
        
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"👉 @{next_player['username']}'s Turn\n"
                 f"Target: <b>{game.current_word_length} letters</b> starting with <b>{game.current_start_letter.upper()}</b>\n"
                 f"⏱️ <b>Time: {turn_time}s</b>",
            parse_mode='HTML'
        )
        game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], application))
            
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Error in timeout handler: {e}")

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
    game.turn_count = 0
    game.current_player_index = 0
    game.eliminated_players = set()
    game.used_words = set()
    
    # Randomize first challenge
    game.current_start_letter = random.choice(string.ascii_lowercase)
    if game.game_mode == 'chaos':
        game.current_word_length = random.randint(3, 12)
    else:
        game.current_word_length = 3

    game.turn_start_time = time.time()
    current_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = current_player['id']

    difficulty_emoji = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
    player_names = ', '.join([str(p['name']) for p in game.players if p.get('name')])
    await update.message.reply_text(
        f"🎮 <b>Game Started!</b>\n"
        f"Mode: <b>{game.game_mode.upper()}</b>\n"
        f"Difficulty: {difficulty_emoji.get(game.difficulty, '🟡')} <b>{game.difficulty.upper()}</b>\n"
        f"Players: {player_names}\n\n"
        f"👉 {str(current_player['name'])}'s turn!\n"
        f"Write a word with exactly <b>{game.current_word_length}</b> letters starting with <b>'{game.current_start_letter.upper()}'</b>\n"
        f"⏱️ <b>Time: {turn_time}s</b>",
        parse_mode='HTML'
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
    user_id = update.effective_user.id

    # Fetch all players to find the user's rank
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"SELECT user_id, username, {category} FROM leaderboard ORDER BY {category} DESC")
    all_players = c.fetchall()
    conn.close()

    if not all_players:
        await update.message.reply_text("🏆 Leaderboard is empty!")
        return

    # Find user's rank
    user_rank = -1
    for idx, (p_id, p_name, p_val) in enumerate(all_players, 1):
        if p_id == user_id:
            user_rank = idx
            break
    
    # Calculate page range (10 per page)
    if user_rank == -1:
        start_idx = 0
        end_idx = 10
    else:
        # Determine which page of 10 the user is on
        page = (user_rank - 1) // 10
        start_idx = page * 10
        end_idx = start_idx + 10

    # Get the slice for the page
    page_players = all_players[start_idx:end_idx]

    category_names = {
        'total_score': 'Total Score',
        'total_words': 'Words Played',
        'best_streak': 'Best Streak',
        'longest_word_length': 'Longest Word'
    }

    text = f"🏆 <b>Leaderboard - {category_names.get(category, 'Total Score')}</b> 🏆\n"
    text += f"<i>Showing ranks {start_idx + 1} - {min(end_idx, len(all_players))}</i>\n\n"
    
    for idx, (p_id, p_name, p_val) in enumerate(page_players, start_idx + 1):
        # Emojis for top 3
        if idx == 1: emoji = "🥇"
        elif idx == 2: emoji = "🥈"
        elif idx == 3: emoji = "🥉"
        else: emoji = f"{idx}."

        # Highlight current user
        if p_id == user_id:
            text += f"👉 <b>{emoji} {p_name} - {p_val}</b> (YOU)\n"
        else:
            text += f"{emoji} <b>{p_name}</b> - {p_val}\n"

    if user_rank != -1:
        text += f"\n👤 Your Rank: <b>#{user_rank}</b>"
    
    text += "\n\n💡 Use: /leaderboard [score/words/streak/longest]"
    await update.message.reply_text(text, parse_mode='HTML')

async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch between Chaos and Nerd game modes"""
    if is_message_stale(update):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in games:
        games[chat_id] = GameState(chat_id=chat_id, application=context.application)
    
    game = games[chat_id]
    
    if game.is_running:
        await update.message.reply_text("❌ Cannot change mode during an active game!")
        return
    
    if not context.args:
        await update.message.reply_text(
            f"🎮 <b>Current Mode: {game.game_mode.upper()}</b>\n\n"
            "🎲 <b>CHAOS</b>\n"
            "• Random letters each turn\n"
            "• Random word lengths (3-12 letters)\n"
            "• Unpredictable & chaotic\n\n"
            "🤓 <b>NERD</b>\n"
            "• Random letters each turn\n"
            "• Word length increases +1 every round\n"
            "• Starts at 3 letters\n\n"
            "Use: /mode [chaos/nerd]",
            parse_mode='HTML'
        )
        return
    
    new_mode = context.args[0].lower()
    if new_mode in ['chaos', 'nerd']:
        game.game_mode = new_mode
        mode_emoji = {'chaos': '🎲', 'nerd': '🤓'}
        await update.message.reply_text(
            f"✅ Mode set to {mode_emoji[new_mode]} <b>{new_mode.upper()}</b>!",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Invalid mode! Use: chaos or nerd")

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
        f"Target: *exactly {game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n"
        f"⏱️ *Time: {turn_time}s*",
        parse_mode='MarkdownV2'
    )
    
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))

async def setbio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /setbio and /bio"""
    if is_message_stale(update): return
    user = update.effective_user
    bio, has_access = db.get_bio(user.id)
    
    if not has_access:
        await update.message.reply_text("❌ You need to purchase 'Bio Access' from the /shop for 500 pts first!")
        return
        
    if not context.args:
        await update.message.reply_text("📝 Usage: /setbio [your text] or /bio [your text]\nMax 40 words.")
        return
        
    bio_text = " ".join(context.args)
    if len(bio_text.split()) > 40:
        await update.message.reply_text("❌ Bio is too long! Max 40 words allowed.")
        return
        
    db.set_bio(user.id, bio_text)
    await update.message.reply_text("✅ Bio updated! To change it again, you'll need to buy another Bio Access.")

async def omnipotent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to grant points or infinity"""
    if is_message_stale(update): return
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    # Check if user is bot owner OR has specific omnipotent permission
    if not db.is_user_omnipotent(user.id):
        await update.message.reply_text("You can't grasp this power! [ACCESS DENIED]")
        return
    
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("❌ Reply to a user's message with /omnipotent [points]\nExample: Reply with /omnipotent 100 or /omnipotent infinite")
        return
    
    target_user = update.message.reply_to_message.from_user
    points = 0
    is_infinite = False
    
    if context.args:
        arg = context.args[0].lower()
        if arg in ['infinite', 'inf', '∞']:
            is_infinite = True
            points = 999999999
        elif arg.isdigit():
            points = int(arg)
        else:
            await update.message.reply_text("❌ Usage: Reply with /omnipotent [points/infinite]")
            return
    else:
        await update.message.reply_text("❌ Usage: Reply with /omnipotent [points]")
        return
    
    db.add_balance(target_user.id, points)
    gift_text = "<b>INFINITE pts</b>" if is_infinite else f"<b>+{points} pts</b>"
    await update.message.reply_text(f"✨ @{target_user.username} received {gift_text} from <b>@{user.username}</b>!", parse_mode='HTML')

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
        owned = inventory.get(boost_type, 0)
        text += f"{details['description']}\n💵 Price: <b>{details['price']} pts</b> - Owned: <b>{owned}</b>\n/buy_{boost_type}\n\n"
    
    text += "<b>🖋️ PERSONAL BIO</b>\n"
    text += "└ 🏷️ Price: <code>500</code> pts | /buy_bio\n"
    text += "<i>Set a custom message on your profile (Max 40 words). Access consumed on use.</i>\n\n"
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
    if "/buy_hint" in message_text: boost_type = 'hint'
    elif "/buy_skip" in message_text: boost_type = 'skip'
    elif "/buy_rebound" in message_text: boost_type = 'rebound'
    elif "/buy_streak" in message_text: boost_type = 'streak'
    elif "/buy_bio" in message_text: boost_type = 'bio'
    elif "/buy_bal_photo" in message_text: boost_type = 'bal_photo'
    
    if not boost_type:
        await update.message.reply_text("❌ Invalid boost! Use: /buy_hint, /buy_skip, /buy_rebound, /buy_streak, /buy_bio, or /buy_bal_photo")
        return
    
    # Handle balance photo separately due to new license logic
    if boost_type == 'bal_photo':
        if db.has_bal_photo_access(user.id):
            await update.message.reply_text("❌ You already have an unused Balance Photo license! Use /setbalpic first.")
            return
        
        price = 1500
        # buy_boost now supports 'bal_photo' directly with the correct logic
        if db.buy_boost(user.id, 'bal_photo', price):
            await update.message.reply_text("✅ <b>Custom Balance Photo Access Purchased!</b>\n\nTo set your photo, reply to any image with <code>/setbalpic</code>.", parse_mode='HTML')
        else:
            balance = db.get_balance(user.id)
            await update.message.reply_text(f"❌ Insufficient balance! Need {price} pts, have {balance} pts")
        return

    price = 500 if boost_type == 'bio' else SHOP_BOOSTS.get(boost_type, {}).get('price', 0)
    if db.buy_boost(user.id, boost_type, price):
        if boost_type == 'bio':
            await update.message.reply_text("✅ <b>Bio Access Purchased!</b>\n\nUse /bio [text] to set your custom profile message (Max 40 words).", parse_mode='HTML')
        else:
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
    
    await update.message.reply_text(f"⏭️ @{user.username} used skip boost\\!\n\n👉 @{next_player['username']}'s Turn\nTarget: *exactly {game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n⏱️ *Time: {turn_time}s*", parse_mode='MarkdownV2')
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
    # Pass preserve_challenge=True to keep the same letter and length
    game.next_turn(preserve_challenge=True)
    next_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    
    await update.message.reply_text(f"🔄 @{user.username} rebounded\\!\n\n👉 @{next_player['username']}'s Turn \\(SAME QUESTION\\)\nTarget: *exactly {game.current_word_length} letters* starting with *'{game.current_start_letter.upper()}'*\n⏱️ *Time: {turn_time}s*", parse_mode='MarkdownV2')
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
    """Admin command to grant points or infinity"""
    if is_message_stale(update): return
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    # Check if user is bot owner OR has specific omnipotent permission
    if not db.is_user_omnipotent(user.id):
        await update.message.reply_text("You can't grasp this power! [ACCESS DENIED]")
        return
    
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("❌ Reply to a user's message with /omnipotent [points]\nExample: Reply with /omnipotent 100 or /omnipotent infinite")
        return
    
    target_user = update.message.reply_to_message.from_user
    points = 0
    is_infinite = False
    
    if context.args:
        arg = context.args[0].lower()
        if arg in ['infinite', 'inf', '∞']:
            is_infinite = True
            points = 999999999
        elif arg.isdigit():
            points = int(arg)
        else:
            await update.message.reply_text("❌ Usage: Reply with /omnipotent [points/infinite]")
            return
    else:
        await update.message.reply_text("❌ Usage: Reply with /omnipotent [points]")
        return
    
    db.add_balance(target_user.id, points)
    gift_text = "<b>INFINITE pts</b>" if is_infinite else f"<b>+{points} pts</b>"
    await update.message.reply_text(f"✨ @{target_user.username} received {gift_text} from <b>@{user.username}</b>!", parse_mode='HTML')

async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Claim daily point reward"""
    if is_message_stale(update): return
    user = update.effective_user
    
    # Get last claim date
    last_claim = db.get_player_last_daily(user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if last_claim == today:
        await update.message.reply_text("⏳ You've already claimed your daily reward today! Come back tomorrow.")
        return
    
    reward = 20
    db.add_balance(user.id, reward)
    db.update_player_last_daily(user.id, today)
    
    await update.message.reply_text(
        f"🎁 <b>Daily Reward!</b>\n\n"
        f"You received <b>{reward} pts</b>!\n"
        f"Current Balance: <b>{db.get_balance(user.id)} pts</b>",
        parse_mode='HTML'
    )

async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transfer points between players"""
    if is_message_stale(update): return
    user = update.effective_user
    
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("❌ Reply to a user's message with /donate [amount]\nExample: Reply to their message with /donate 100")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    if target_user.id == user.id:
        await update.message.reply_text("❌ You cannot donate to yourself!")
        return
    
    if target_user.is_bot:
        await update.message.reply_text("❌ You cannot donate to bots!")
        return

    amount = 0
    if context.args and context.args[0].isdigit():
        amount = int(context.args[0])
    else:
        await update.message.reply_text("❌ Usage: Reply to a message with /donate [amount]\nExample: /donate 100")
        return
    
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0!")
        return
    
    current_balance = db.get_balance(user.id)
    if current_balance < amount:
        await update.message.reply_text(f"❌ Insufficient balance! You have {current_balance} pts.")
        return
    
    # Perform transfer
    db.add_balance(user.id, -amount)
    db.add_balance(target_user.id, amount)
    
    # Ensure target exists in DB
    db.ensure_player_exists(target_user.id, target_user.first_name)
    
    await update.message.reply_text(
        f"💸 <b>Donation Successful!</b>\n\n"
        f"👤 <b>From:</b> {user.first_name}\n"
        f"👤 <b>To:</b> {target_user.first_name}\n"
        f"💰 <b>Amount:</b> {amount} pts\n\n"
        f"<i>How generous!</i>",
        parse_mode='HTML'
    )

async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View only unlocked achievements/titles"""
    user = update.effective_user
    unlocked_list = db.get_unlocked_titles(user.id)
    unlocked_stages = {}
    for entry in unlocked_list:
        if ':' in entry:
            k, s = entry.split(':')
            unlocked_stages[k] = int(s)
            
    active = db.get_active_title(user.id)
    
    text = "🏆 <b>Your Unlocked Titles</b>\n\n"
    has_any = False
    
    # Check exclusive first
    if user.id == BOT_OWNER_ID:
        text += f"✨ <b>KAMI</b>\n  <i>Exclusive Divine Title</i>\n\n"
        has_any = True
        
    for title_key, title_data in TITLES.items():
        if title_data.get('exclusive'): continue
            
        stage = unlocked_stages.get(title_key, 0)
        if stage > 0:
            has_any = True
            text += f"<b>{title_data['display']}</b> "
            text += STAGES[stage]['display']
            if title_key == active:
                text += " ⭐ (Equipped)"
            text += f"\n  <i>Current Level: {stage}/5</i>\n\n"
    
    if not has_any:
        text += "<i>No titles unlocked yet. Keep playing to earn achievements!</i>\n"
    
    text += "\n/progress - Check what you need next\n/settitle [title] - Change your title"
    await update.message.reply_text(text, parse_mode='HTML')

async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View upcoming requirements and milestones"""
    user = update.effective_user
    unlocked_list = db.get_unlocked_titles(user.id)
    unlocked_stages = {}
    for entry in unlocked_list:
        if ':' in entry:
            k, s = entry.split(':')
            unlocked_stages[k] = int(s)
            
    text = "📊 <b>Title Progress & Requirements</b>\n\n"
    for title_key, title_data in TITLES.items():
        if title_data.get('exclusive'): continue
            
        current_stage = unlocked_stages.get(title_key, 0)
        text += f"<b>{title_data['display']}</b> "
        
        # Draw progress bar
        for s in range(1, 6):
            text += STAGES[s]['display'] if s <= current_stage else "▫️"
        
        text += "\n"
        if current_stage < 5:
            next_stage = current_stage + 1
            req_val = int(title_data['base_req'] * STAGES[next_stage]['multiplier'])
            desc = title_data['desc'].format(req=req_val)
            text += f"  <i>Next Stage {next_stage}: {desc}</i>\n"
        else:
            text += "  <i>MAX LEVEL REACHED!</i> 💎\n"
        text += "\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = user

    stats = db.get_player_stats(target_user.id)
    if not stats:
        await update.message.reply_text("👤 User has no record yet!")
        return

    unlocked_list = db.get_unlocked_titles(target_user.id)
    unlocked_stages = {}
    total_stages = 0
    for entry in unlocked_list:
        if ':' in entry:
            try:
                k, s = entry.split(':')
                val = int(s)
                unlocked_stages[k] = val
                total_stages += val
            except (ValueError, IndexError):
                continue
    
    active_key = db.get_active_title(target_user.id)
    title_display = ""
    is_kami = False
    
    if active_key in TITLES:
        if TITLES[active_key].get('exclusive'):
            title_display = f"✨ <b>{TITLES[active_key]['display']}</b> ✨"
            is_kami = True
        else:
            stage = unlocked_stages.get(active_key, 1)
            stage_data = STAGES.get(stage, STAGES[1])
            title_display = f"{stage_data['color']} <b>{TITLES[active_key]['display']} {stage_data['display']}</b>"
    
    # Scale border aesthetics with total stages
    if is_kami:
        beauty_border = "✦ . ✦ . ✦ . ✦ . ✦ . ✦ . ✦"
        profile_header = "🌌 <b>𝐂𝐄𝐋𝐄𝐒𝐓𝐈𝐀𝐋 𝐄𝐍𝐓𝐈𝐓𝐘</b> 🌌"
        stats_header = "✧ <b>𝐃𝐈𝐕𝐈𝐍𝐄 𝐄𝐒𝐒𝐄𝐍𝐂𝐄</b> ✧"
    elif total_stages >= 20:
        beauty_border = "💠 ═══ 💠 ═══ 💠 ═══ 💠"
        profile_header = "👑 <b>𝐄𝐋𝐈𝐓𝐄 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 👑"
        stats_header = "📊 <b>𝐆𝐀𝐌𝐄 𝐒𝐓𝐀𝐓𝐈𝐒𝐓𝐈𝐂𝐒</b>"
    elif total_stages >= 15:
        beauty_border = "✨ ═══ ✨ ═══ ✨ ═══ ✨"
        profile_header = "💎 <b>𝐌𝐀𝐒𝐓𝐄𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 💎"
        stats_header = "📊 <b>𝐆𝐀𝐌𝐄 𝐒𝐓𝐀𝐓𝐈𝐒𝐓𝐈𝐂𝐒</b>"
    elif total_stages >= 10:
        beauty_border = "🔶 ═══ 🔶 ═══ 🔶 ═══ 🔶"
        profile_header = "⚔️ <b>𝐖𝐀𝐑𝐑𝐈𝐎𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> ⚔️"
        stats_header = "📊 <b>𝐆𝐀𝐌𝐄 𝐒𝐓𝐀𝐓𝐈𝐒𝐓𝐈𝐂𝐒</b>"
    elif total_stages >= 5:
        beauty_border = "🔹 ═══ 🔹 ═══ 🔹 ═══ 🔹"
        profile_header = "🛡️ <b>𝐀𝐃𝐕𝐄𝐍𝐓𝐔𝐑𝐄𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 🛡️"
        stats_header = "📊 <b>𝐆𝐀𝐌𝐄 𝐒𝐓𝐀𝐓𝐈𝐒𝐓𝐈𝐂𝐒</b>"
    else:
        beauty_border = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
        profile_header = "👤 <b>𝐏𝐋𝐀𝐘𝐄𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b>"
        stats_header = "📊 <b>𝐆𝐀𝐌𝐄 𝐒𝐓𝐀𝐓𝐈𝐒𝐓𝐈𝐂𝐒</b>"

    text = f"{beauty_border}\n"
    text += f"{profile_header}\n"
    text += f"{beauty_border}\n\n"
    
    text += f"👤 <b>𝐍𝐚𝐦𝐞:</b> <code>{target_user.first_name}</code>\n"
    if title_display:
        text += f"🎖️ <b>𝐓𝐢𝐭𝐥𝐞:</b> {title_display}\n"
    text += f"💰 <b>𝐁𝐚𝐥𝐚𝐧𝐜𝐞:</b> <code>{db.get_balance(target_user.id)}</code> pts\n\n"
    
    # Bio section (Enhanced display)
    bio_data, _ = db.get_bio(target_user.id)
    if bio_data:
        text += f"📜 <b>𝐁𝐢𝐨:</b>\n<i>« {bio_data} »</i>\n\n"
    elif str(target_user.id) == str(user.id):
        text += f"💡 <i>Tip: Use /buy_bio to add a personal message!</i>\n\n"
    
    text += f"{stats_header}\n"
    text += f"┣ 𝐒𝐜𝐨𝐫𝐞: <code>{stats[7]}</code>\n"
    text += f"┣ 𝐖𝐨𝐫𝐝𝐬: <code>{stats[2]}</code>\n"
    text += f"┣ 𝐒𝐭𝐫𝐞𝐚𝐤: <code>{stats[6]}</code>\n"
    text += f"┣ 𝐋𝐨𝐧𝐠𝐞𝐬𝐭: <code>{stats[5]}</code>\n"
    text += f"┗ 𝐆𝐚𝐦𝐞𝐬: <code>{stats[3]}</code>\n\n"
    
    if not is_kami:
        text += f"🏆 <b>𝐌𝐀𝐒𝐓𝐄𝐑𝐘 𝐏𝐑𝐎𝐆𝐑𝐄𝐒𝐒</b>\n"
        for t_key, t_data in TITLES.items():
            if t_data.get('exclusive'): continue
            stage = unlocked_stages.get(t_key, 0)
            # Use cleaner progress blocks
            filled = "⬛" * stage
            empty = "⬜" * (5 - stage)
            text += f"{t_data['display'].split()[0]} {filled}{empty} ({stage}/5)\n"
    else:
        text += f"🌟 <b>𝐒𝐔𝐏𝐑𝐄𝐌𝐄 𝐀𝐔𝐓𝐇𝐎𝐑𝐈𝐓𝐘</b> 🌟\n"
        text += f"<i>Absolute ruler of the word domain.</i>\n"
    
    text += f"\n{beauty_border}"
    
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

async def vscpu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start 1v1 game vs CPU opponent"""
    if is_message_stale(update): return
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id in games and games[chat_id].is_running:
        await update.message.reply_text("❌ A game is already running! Use /stop first.")
        return
    
    difficulty = 'medium'
    if context.args:
        difficulty = context.args[0].lower()
        if difficulty not in ['easy', 'medium', 'hard']:
            difficulty = 'medium'
    
    game = GameState(chat_id=chat_id, application=context.application)
    game.is_cpu_game = True
    game.cpu_difficulty = difficulty
    game.is_running = True
    game.group_owner = user.id
    
    display_name = str(user.first_name or user.username or "Player").strip()
    if not display_name or display_name == "None":
        display_name = "Player"
    username_to_store = (user.username if user.username else display_name).lstrip('@')
    
    game.players = [
        {'id': user.id, 'name': display_name, 'username': username_to_store},
        {'id': 999999, 'name': '🤖 CPU', 'username': 'cpu'}
    ]
    game.initialize_player_stats(user.id)
    game.initialize_player_stats(999999)
    db.ensure_player_exists(user.id, username_to_store)
    games[chat_id] = game
    
    # Initialize first challenge
    game.current_start_letter = random.choice(string.ascii_lowercase)
    if game.game_mode == 'chaos':
        game.current_word_length = random.randint(3, 12)
    else:
        game.current_word_length = 3
        
    turn_time = game.get_turn_time()
    game.current_turn_user_id = user.id
    
    difficulty_emoji = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
    await update.message.reply_text(
        f"🎮 <b>1v1 vs CPU 🤖</b>\n"
        f"Difficulty: {difficulty_emoji.get(difficulty, '🟡')} <b>{difficulty.upper()}</b>\n\n"
        f"👉 {display_name}'s Turn\n"
        f"Target: <b>exactly {game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>\n"
        f"⏱️ <b>Time: {turn_time}s</b>",
        parse_mode='HTML'
    )
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, user.id, context.application))

async def cpu_turn(chat_id: int, application):
    """Handle CPU player turn"""
    if chat_id not in games:
        return
    game = games[chat_id]
    
    # Wait a bit to simulate "thinking"
    await asyncio.sleep(2)
    
    # Ensure it's actually CPU's turn
    if game.players[game.current_player_index]['id'] != 999999:
        return

    cpu_word = game.get_cpu_word()
    
    if not cpu_word:
        await application.bot.send_message(chat_id, "🤖 CPU forfeit! (No valid words)")
        game.eliminated_players.add(999999)
    else:
        game.used_words.add(cpu_word)
        game.increment_streak(999999)
        await application.bot.send_message(chat_id, f"🤖 CPU played: <b>{cpu_word}</b> (+{len(cpu_word)})", parse_mode='HTML')
    
    # Check for winner BEFORE next turn
    alive_players = [p for p in game.players if p['id'] not in game.eliminated_players]
    if len(alive_players) <= 1:
        winner = alive_players[0] if alive_players else None
        if winner:
            await application.bot.send_message(chat_id, f"🏆 <b>{winner['name']} WINS!</b>", parse_mode='HTML')
            if winner['id'] != 999999:
                db.increment_games_played(winner['id'])
        game.reset()
        if chat_id in games:
            del games[chat_id]
        return

    game.next_turn()
    
    next_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    
    await application.bot.send_message(
        chat_id,
        f"👉 @{next_player['username']}'s Turn\n"
        f"Target: <b>exactly {game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>\n"
        f"⏱️ <b>Time: {turn_time}s</b>",
        parse_mode='HTML'
    )
    
    # Start timeout task for the next player
    game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], application))
    
    # If the next player is also CPU (unlikely in 1v1 but good for safety), trigger it
    if next_player['id'] == 999999:
        asyncio.create_task(cpu_turn(chat_id, application))

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
        f"Target: <b>exactly {game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>\n"
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

import os
from PIL import Image

async def setbalpic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow users to set their custom /bal picture after buying access"""
    if is_message_stale(update): return
    user = update.effective_user
    
    if not db.has_bal_photo_access(user.id):
        await update.message.reply_text("❌ You need to purchase 'Custom Balance Photo' from the /shop for 1500 pts first!")
        return
    
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ Please reply to an image with /setbalpic to set your balance background.")
        return
    
    photo = update.message.reply_to_message.photo[-1].file_id
    db.set_custom_bal_photo(user.id, photo)
    # Re-enable the license by setting has_bal_photo_access back to 1
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE titles SET has_bal_photo_access = 1 WHERE user_id = ?", (user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Your custom /bal picture has been set!")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check shop points balance"""
    if is_message_stale(update): return
    user = update.effective_user
    
    # Use the dedicated get_balance method to get shop inventory points
    balance = db.get_balance(user.id)
    
    is_kami = (user.id == BOT_OWNER_ID)
    custom_photo = db.get_custom_bal_photo(user.id)
    
    if is_kami:
        # Check if kami_balance_compressed.jpg exists, otherwise use original or try to compress
        image_path = "attached_assets/Picsart_25-12-25_07-48-43-245_1766820109612.png"
        compressed_path = "attached_assets/kami_balance_compressed.jpg"
        
        caption = (
            f"✨ <b>KAMI BALANCE</b> ✨\n\n"
            f"👤 <b>Developer:</b> {user.first_name}\n"
            f"💰 <b>Shop Points:</b> {balance} pts\n\n"
            f"<i>The ultimate power resides here.</i>"
        )
        
        try:
            # Check if file exists before trying to open it
            final_path = compressed_path if os.path.exists(compressed_path) else image_path
            
            if os.path.exists(final_path):
                with open(final_path, 'rb') as photo_file:
                    await update.message.reply_photo(
                        photo=photo_file,
                        caption=caption,
                        parse_mode='HTML'
                    )
            else:
                # Fallback to text if image is missing
                await update.message.reply_text(caption, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Error sending kami balance image: {e}")
            await update.message.reply_text(caption, parse_mode='HTML')
    elif custom_photo:
        caption = (
            f"💰 <b>Your Balance</b>\n\n"
            f"👤 <b>Player:</b> {user.first_name}\n"
            f"💎 <b>Shop Points:</b> {balance} pts\n\n"
            f"Use /shop to spend your points!"
        )
        try:
            await update.message.reply_photo(photo=custom_photo, caption=caption, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Error sending custom balance photo: {e}")
            await update.message.reply_text(caption, parse_mode='HTML')
    else:
        await update.message.reply_text(
            f"💰 <b>Your Balance</b>\n\n"
            f"👤 <b>Player:</b> {user.first_name}\n"
            f"💎 <b>Shop Points:</b> {balance} pts\n\n"
            f"Use /shop to spend your points!",
            parse_mode='HTML'
        )

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
    
    unlocked_list = db.get_unlocked_titles(target_user_id)
    unlocked_stages = {}
    total_stages = 0
    for entry in unlocked_list:
        if ':' in entry:
            try:
                k, s = entry.split(':')
                val = int(s)
                unlocked_stages[k] = val
                total_stages += val
            except (ValueError, IndexError):
                continue
    
    unlocked_list = db.get_unlocked_titles(target_user_id)
    unlocked_stages = {}
    total_stages = 0
    for entry in unlocked_list:
        if ':' in entry:
            try:
                k, s = entry.split(':')
                val = int(s)
                unlocked_stages[k] = val
                total_stages += val
            except (ValueError, IndexError):
                continue

    # Determine active title and Divine status
    active_key = db.get_active_title(target_user_id)
    title_display = ""
    is_kami = False
    
    # Title Themes Definition
    TITLE_THEMES = {
        'legend': {
            'header': "👑 <b>𝐋𝐄𝐆𝐄𝐍𝐃𝐀𝐑𝐘 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 👑",
            'border': "🌟 ━━━━━━━━━━━ 🌟",
            'symbol': "🏆",
            'decoration': "<i>『 The history remembers your name. 』</i>"
        },
        'warrior': {
            'header': "⚔️ <b>𝐖𝐀𝐑𝐑𝐈𝐎𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> ⚔️",
            'border': "🩸 ━━━━━━━━━━━ 🩸",
            'symbol': "🛡️",
            'decoration': "<i>『 Strength and honor above all. 』</i>"
        },
        'sage': {
            'header': "🧙 <b>𝐒𝐀𝐆𝐄 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 🧙",
            'border': "📜 ━━━━━━━━━━━ 📜",
            'symbol': "🔮",
            'decoration': "<i>『 Wisdom is the ultimate weapon. 』</i>"
        },
        'phoenix': {
            'header': "🔥 <b>𝐏𝐇𝐎𝐄𝐍𝐈𝐗 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 🔥",
            'border': "🌋 ━━━━━━━━━━━ 🌋",
            'symbol': "🐦‍🔥",
            'decoration': "<i>『 From the ashes, I shall rise. 』</i>"
        },
        'shadow': {
            'header': "🌑 <b>𝐒𝐇𝐀𝐃𝐎𝐖 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 🌑",
            'border': "🕶️ ━━━━━━━━━━━ 🕶️",
            'symbol': "🗝️",
            'decoration': "<i>『 Silent as a whisper, deadly as night. 』</i>"
        },
        'kami': {
            'header': "✨ <b>𝐃𝐈𝐕𝐈𝐍𝐄 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> ✨",
            'border': "✦ ━━━━━━━━━━━ ✦",
            'symbol': "🌌",
            'decoration': "<i>『 Honor is not a title, it is a soul. 』</i>"
        }
    }

    if active_key in TITLES:
        if TITLES[active_key].get('exclusive'):
            title_display = f"<b>{TITLES[active_key]['display']}</b> ✨"
            is_kami = True
        else:
            stage = unlocked_stages.get(active_key, 1)
            stage_data = STAGES.get(stage, STAGES[1])
            title_display = f"{stage_data['color']} <b>{TITLES[active_key]['display']} {stage_data['display']}</b>"
    elif target_user_id == BOT_OWNER_ID:
        active_key = 'kami'
        title_display = f"<b>{TITLES['kami']['display']}</b> ✨"
        is_kami = True

    # Aesthetic redesign based on Title Theme
    theme = TITLE_THEMES.get(active_key)
    
    if theme:
        beauty_border = theme['border']
        profile_header = theme['header']
        theme_decoration = theme['decoration']
    else:
        # Fallback to level-based scaling if no title or generic title
        if total_stages >= 20:
            beauty_border = "💠 ━━━━━━━━━━━ 💠"
            profile_header = "👑 <b>𝐄𝐋𝐈𝐓𝐄 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 👑"
        elif total_stages >= 15:
            beauty_border = "✨ ━━━━━━━━━━━ ✨"
            profile_header = "💎 <b>𝐌𝐀𝐒𝐓𝐄𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 💎"
        elif total_stages >= 10:
            beauty_border = "🔶 ━━━━━━━━━━━ 🔶"
            profile_header = "⚔️ <b>𝐖𝐀𝐑𝐑𝐈𝐎𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> ⚔️"
        elif total_stages >= 5:
            beauty_border = "🔹 ━━━━━━━━━━━ 🔹"
            profile_header = "🛡️ <b>𝐀𝐃𝐕𝐄𝐍𝐓𝐔𝐑𝐄𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b> 🛡️"
        else:
            beauty_border = "━━━━━━━━━━━━━━━"
            profile_header = "👤 <b>𝐏𝐋𝐀𝐘𝐄𝐑 𝐏𝐑𝐎𝐅𝐈𝐋𝐄</b>"
        theme_decoration = ""

    profile_text = f"<code>{beauty_border}</code>\n"
    profile_text += f"{profile_header}\n"
    profile_text += f"<code>{beauty_border}</code>\n\n"
    
    profile_text += f"<b>NAME:</b> <code>{target_username}</code>\n"
    if title_display:
        profile_text += f"<b>TITLE:</b> {title_display}\n"
    else:
        profile_text += f"<b>TITLE:</b> 🔒 Locked\n"
    
    if theme_decoration:
        profile_text += f"{theme_decoration}\n\n"
    else:
        profile_text += "\n"
    
    # Bio section (Enhanced display)
    bio_data, _ = db.get_bio(target_user_id)
    if bio_data:
        profile_text += f"📝 <b>BIO</b>\n"
        profile_text += f"« <i>{bio_data}</i> »\n\n"
    elif target_user_id == user.id:
        profile_text += f"💡 <i>Tip: Use /buy_bio to add a personal message!</i>\n\n"
    
    # Statistics section (Requested layout)
    profile_text += f"📊 <b>STATISTICS</b>\n"
    profile_text += f" ┣ 🎯 Score: <code>{stats[7]}</code>\n"
    profile_text += f" ┣ 📝 Words: <code>{stats[2]}</code>\n"
    profile_text += f" ┣ ⚡ Streak: <code>{stats[6]}</code>\n"
    profile_text += f" ┣ 🎮 Games: <code>{stats[3]}</code>\n"
    profile_text += f" ┣ 📏 Longest: <code>{stats[4]}</code> ({stats[5]}L)\n"
    profile_text += f" ┗ 📈 Average: <code>{stats[8]:.1f}</code>\n\n"

    # Auto-unlock titles on every profile view to ensure progress is tracked
    db.auto_unlock_titles(target_user_id)
    
    if not is_kami:
        profile_text += f"🏆 <b>MASTERY LEVELS</b>\n"
        for t_key, t_data in TITLES.items():
            if t_data.get('exclusive'): continue
            
            stage = unlocked_stages.get(t_key, 0)
            
            # Progress tracking (X/Y)
            if stage < 5:
                next_stage = stage + 1
                req_val = int(t_data['base_req'] * STAGES[next_stage]['multiplier'])
                
                # Get current stat value for comparison
                player_stats = db.get_player_stats(target_user_id)
                current_val = 0
                if t_key == 'legend': current_val = player_stats[7] # total_score
                elif t_key == 'warrior': current_val = player_stats[6] # best_streak
                elif t_key == 'sage': current_val = player_stats[2] # total_words
                elif t_key == 'phoenix': current_val = player_stats[3] # games_played
                elif t_key == 'shadow': current_val = player_stats[5] # longest_word
                
                progress_str = f"({current_val}/{req_val})"
            else:
                progress_str = "(MAX)"
                
            bar = "▰" * stage + "▱" * (5 - stage)
            profile_text += f" {t_data['display'][:2]} {bar} <code>{progress_str}</code>\n"
    else:
        profile_text += f"🌌 <b>CELESTIAL MASTERY</b>\n"
        profile_text += f"<i>『ƈʀɨʍֆօռ♦』alone is the honored one.</i>\n"
    
    profile_text += f"\n<code>{beauty_border}</code>"
    
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

async def grant_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot owner command to grant /omnipotent access"""
    if is_message_stale(update): return
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("❌ Reply to a user with /grant or /revoke")
        return

    target = update.message.reply_to_message.from_user
    command = update.message.text.split()[0].lower()
    
    if "grant" in command:
        db.set_user_omnipotent(target.id, True)
        await update.message.reply_text(f"✅ Granted omnipotent powers to @{target.username}")
    else:
        db.set_user_omnipotent(target.id, False)
        await update.message.reply_text(f"❌ Revoked omnipotent powers from @{target.username}")

async def tagall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mention all known members in this chat"""
    if is_message_stale(update): return
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    # Check if user is bot owner OR has specific omnipotent permission OR is an admin
    is_owner = (user.id == BOT_OWNER_ID)
    is_authorized = db.is_user_omnipotent(user.id)
    is_admin = False
    
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = chat_member.status in ['creator', 'administrator']
    except:
        pass

    if not (is_owner or is_authorized or is_admin):
        await update.message.reply_text("❌ Only the bot owner, authorized users, or admins can use .tagall!")
        return

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Get all unique users seen in this specific chat
    c.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("❌ No members tracked in this chat yet!")
        return
        
    tag_msg = "📢 <b>ATTENTION EVERYONE!</b> 📢\n\n"
    usernames = []
    for r in rows:
        if r[0]:
            name = r[0]
            if not name.startswith('@'):
                usernames.append(f"@{name}")
            else:
                usernames.append(name)
    
    tag_msg += " ".join(list(set(usernames))) # Unique tags
    
    custom_msg = " ".join(context.args) if context.args else "Wake up! A new challenge awaits!"
    tag_msg += f"\n\n💬 {custom_msg}"
    
    await update.message.reply_text(tag_msg, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Complete gameplay guide and rules"""
    help_text = (
        "🎮 <b>INFINITE WORD GAME - MASTER GUIDE</b> 🎮\n\n"
        "<b>1. BASIC RULES</b>\n"
        "• Submit words matching the target letter and length.\n"
        "• Words must exist in the 370K+ word dictionary.\n"
        "• You cannot reuse words already played in the same game.\n"
        "• Points = Word Length. Higher length = More points!\n\n"
        "<b>2. GAME MODES</b>\n"
        "🤓 <b>NERD (Progressive):</b> Word length increases +1 every round. Starts at 3.\n"
        "🎲 <b>CHAOS (Random):</b> Every turn has a completely random length (3-12).\n"
        "🤖 <b>VS CPU:</b> 1v1 battle against the bot with 3 difficulty levels.\n"
        "💪 <b>PRACTICE:</b> Solo training to build your vocabulary and speed.\n\n"
        "<b>3. SHOP & BOOSTS</b>\n"
        "📖 <b>HINT (80 pts):</b> Shows 3 possible words for the current target.\n"
        "⏭️ <b>SKIP (150 pts):</b> Skip your turn without point penalty.\n"
        "🔄 <b>REBOUND (250 pts):</b> Skip and pass the same target to the next player!\n\n"
        "<b>4. TITLES & ACHIEVEMENTS</b>\n"
        "Unlock badges like 👑 <b>LEGEND</b>, ⚔️ <b>WARRIOR</b>, or 🧙 <b>SAGE</b> by reaching milestones. "
        "Use /achievements to see them and /settitle to equip one!\n\n"
        "<b>5. STREAKS</b>\n"
        "Build a 3+ streak to get 🔥 <b>STREAK</b> bonuses and show off on the leaderboard!\n\n"
        "<i>Compete, earn points, and climb the global leaderboard!</i>\n\n"
        "✨ <b>Developed by 『ƈʀɨʍֆօռ♦』</b> ✨"
    )
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

    except Exception as e:
        logger.error(f"Error processing word '{word}': {str(e)}", exc_info=True)
        await update.message.reply_text(f"❌ Error processing your word. Try again.")
        game.used_words.discard(word)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler for word game and member tracking"""
    if is_message_stale(update): return
    
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    # Track member for .tagall
    if not user.is_bot:
        username = user.username or user.first_name or "Player"
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO chat_members (chat_id, user_id, username) VALUES (?, ?, ?)",
                 (chat_id, user.id, username))
        conn.commit()
        conn.close()

    if chat_id not in games or not update.message or not update.message.text: return
    
    game = games[chat_id]
    user = update.effective_user
    msg_text = (update.message.text or "").lower()

    # Admin bypass for commands that should always work
    if msg_text.startswith(('/omnipotent', '/bio', '/setbio', '/buy_', '/bal', '/balance', '/mystats', '/profile', '/leaderboard')):
        return

    if not game.is_running: return

    # Turn Validation & Type-Safe ID Check
    current_player = game.players[game.current_player_index]
    
    # Normalize IDs to strings for comparison
    msg_user_id = str(user.id)
    target_user_id = str(current_player['id'])

    if msg_user_id != target_user_id:
        # Prevent "Turn Stealing" - Log attempts from other players
        active_ids = [str(p['id']) for p in game.players if p['id'] not in game.eliminated_players]
        if msg_user_id in active_ids:
            logger.warning(f"Turn intercept blocked: {user.first_name} ({msg_user_id}) tried to play during {current_player.get('first_name', 'target')}'s ({target_user_id}) turn.")
        return

    word_raw = update.message.text.strip()
    # Check if the message contains spaces - game answers are always single words
    if ' ' in word_raw:
        return

    word = word_raw.lower()
    
    # Validation
    if len(word) != game.current_word_length:
        await update.message.reply_text(f"❌ Word must be exactly {game.current_word_length} letters! Try again.")
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

    # Process the turn logic FIRST to avoid any state issues
    game.cancel_timeout()
    game.used_words.add(word)
    game.increment_streak(user.id)
    current_streak = game.get_streak(user.id)
    
    # Update word stats and leaderboard immediately
    if not game.is_practice:
        player_name = user.first_name or user.username or "Player"
        try:
            db.update_word_stats(user.id, player_name, word, current_streak)
        except Exception as db_err:
            logger.error(f"Database error: {db_err}")

    # Check for newly unlocked titles after stats update
    newly_unlocked = db.auto_unlock_titles(user.id)
    if newly_unlocked:
        unlock_msg = "🎉 <b>NEW TITLES UNLOCKED!</b>\n\n"
        for title_key in newly_unlocked:
            if title_key in TITLES:
                unlock_msg += f"✨ {TITLES[title_key]['display']}\n"
        await update.message.reply_text(unlock_msg, parse_mode='HTML')

    difficulty_increased = game.next_turn()
    
    msg_text = f"✅ '{word}' <b>(+{len(word)})</b>"
    if current_streak >= 3:
        msg_text += f"\n🔥 <b>{current_streak} STREAK!</b> You're on fire!"
    msg_text += "\n\n"
    
    if difficulty_increased:
        msg_text += f"⏱️ <b>Time reduced!</b> Difficulty level {game.difficulty_level}\n\n"
    
    next_player = game.players[game.current_player_index]
    turn_time = game.get_turn_time()
    game.current_turn_user_id = next_player['id']
    
    if game.is_practice:
        msg_text += f"💪 <b>Next Challenge:</b>\n"
        msg_text += f"Target: <b>exactly {game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>\n"
        msg_text += f"⏱️ <b>Time: {turn_time}s</b>"
    elif game.is_cpu_game and next_player['id'] == 999999:
        msg_text += f"🤖 <b>CPU's Turn...</b>"
    else:
        msg_text += f"👉 @{next_player['username']}'s Turn\n"
        msg_text += f"Target: <b>exactly {game.current_word_length} letters</b> starting with <b>'{game.current_start_letter.upper()}'</b>\n"
        msg_text += f"⏱️ <b>Time: {turn_time}s</b>"

    await update.message.reply_text(msg_text, parse_mode='HTML')
    
    # CPU turn handler
    if game.is_cpu_game and next_player['id'] == 999999:
        # IMPORTANT: Run CPU turn in background
        asyncio.create_task(cpu_turn(chat_id, context.application))
    else:
        game.timeout_task = asyncio.create_task(handle_turn_timeout(chat_id, next_player['id'], context.application))


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
                application.add_handler(CommandHandler("mode", mode_command))
                application.add_handler(CommandHandler("difficulty", difficulty))
                application.add_handler(CommandHandler("stop", stop_game))
                application.add_handler(CommandHandler("forfeit", forfeit_command))
                application.add_handler(CommandHandler("mystats", mystats_command))
                application.add_handler(CommandHandler("leaderboard", leaderboard))
                application.add_handler(CommandHandler("shop", shop_command))
                application.add_handler(CommandHandler("buy_hint", buy_boost_command))
                application.add_handler(CommandHandler("buy_skip", buy_boost_command))
                application.add_handler(CommandHandler("buy_rebound", buy_boost_command))
                application.add_handler(CommandHandler("buy_streak", buy_boost_command))
                application.add_handler(CommandHandler("buy_bio", buy_boost_command))
                application.add_handler(CommandHandler("buy_bal_photo", buy_boost_command))
                application.add_handler(CommandHandler("hint", hint_boost_command))
                application.add_handler(CommandHandler("skip", skip_boost_command))
                application.add_handler(CommandHandler("skip_boost", skip_boost_command))
                application.add_handler(CommandHandler("rebound", rebound_boost_command))
                application.add_handler(CommandHandler("inventory", inventory_command))
                application.add_handler(CommandHandler("omnipotent", omnipotent_command))
                application.add_handler(CommandHandler("bio", setbio_command))
                application.add_handler(CommandHandler("setbio", setbio_command))
                application.add_handler(CommandHandler("donate", donate_command))
                application.add_handler(CommandHandler("daily", daily_command))
                application.add_handler(CommandHandler("authority", authority_command))
                application.add_handler(CommandHandler("achievements", achievements_command))
                application.add_handler(CommandHandler("settitle", settitle_command))
                application.add_handler(CommandHandler("mytitle", mytitle_command))
                application.add_handler(CommandHandler("progress", progress_command))
                application.add_handler(CommandHandler("profile", profile_command))
                application.add_handler(CommandHandler("practice", practice_command))
                application.add_handler(CommandHandler("vscpu", vscpu_command))
                application.add_handler(CommandHandler("balance", balance_command))
                application.add_handler(CommandHandler("bal", balance_command))
                application.add_handler(CommandHandler("groupdesc", groupdesc_command))
                application.add_handler(CommandHandler("grant", grant_permission))
                application.add_handler(CommandHandler("revoke", grant_permission))
                application.add_handler(CommandHandler("setbalpic", setbalpic_command))
                application.add_handler(MessageHandler(filters.Regex(r'^\.tagall'), tagall_command))
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