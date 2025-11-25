import logging
import random
import sqlite3
import os
import asyncio
from typing import List, Dict, Set

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
        # Create table if not exists
        c.execute('''
            CREATE TABLE IF NOT EXISTS leaderboard (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                wins INTEGER DEFAULT 0,
                games_played INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()

    def update_score(self, user_id, username):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        # Check if user exists
        c.execute("SELECT * FROM leaderboard WHERE user_id=?", (user_id,))
        entry = c.fetchone()
        
        if entry:
            c.execute("UPDATE leaderboard SET wins = wins + 1, username = ? WHERE user_id=?", (username, user_id))
        else:
            c.execute("INSERT INTO leaderboard (user_id, username, wins, games_played) VALUES (?, ?, 1, 1)", (user_id, username))
        conn.commit()
        conn.close()

    def get_top_players(self, limit=10):
        conn = sqlite3.connect(self.db_name)
        c = conn.cursor()
        c.execute("SELECT username, wins FROM leaderboard ORDER BY wins DESC LIMIT ?", (limit,))
        data = c.fetchall()
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
        self.current_word_length = 3
        self.used_words = set()
        self.turn_count = 0

    def next_turn(self):
        self.current_player_index = (self.current_player_index + 1) % len(self.players)
        self.turn_count += 1
        
        # Increase difficulty (length) every full round
        if self.turn_count > 0 and self.turn_count % len(self.players) == 0:
            self.current_word_length += 1
            if self.current_word_length > 15: 
                self.current_word_length = 15

        # Pick random letter
        import string
        self.current_start_letter = random.choice(string.ascii_lowercase)

# Key: chat_id, Value: GameState
games: Dict[int, GameState] = {}
db = DatabaseManager(DB_FILE)

# ==========================================
# BOT COMMANDS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the Infinite Word Game!\n\n"
        "Commands:\n"
        "/lobby - Open a new game lobby\n"
        "/join - Join the lobby\n"
        "/begin - Start the game (needs 2+ players)\n"
        "/stop - Stop the current game\n"
        "/leaderboard - Show top winners"
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

    game.is_lobby_open = False
    game.is_running = True
    
    import string
    game.current_start_letter = random.choice(string.ascii_lowercase)
    current_player = game.players[game.current_player_index]

    await update.message.reply_text(
        f"🎮 <b>Game Started!</b>\n"
        f"Players: {', '.join([p['name'] for p in game.players])}\n\n"
        f"👉 <b>{current_player['name']}</b>'s turn!\n"
        f"Write a <b>{game.current_word_length}-letter</b> word starting with <b>'{game.current_start_letter.upper()}'</b>.",
        parse_mode='HTML'
    )

async def stop_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in games:
        games[chat_id].reset()
        await update.message.reply_text("🛑 Game stopped.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = db.get_top_players()
    if not top:
        await update.message.reply_text("🏆 Leaderboard is empty!")
        return
    
    text = "🏆 <b>Global Leaderboard</b> 🏆\n\n"
    for idx, (name, wins) in enumerate(top, 1):
        text += f"{idx}. {name} - {wins} wins\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in games or not update.message or not update.message.text: return
    
    game = games[chat_id]
    if not game.is_running: return

    user = update.effective_user
    current_player = game.players[game.current_player_index]

    if user.id != current_player['id']: return 

    word = update.message.text.strip().lower()

    # Validations
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

    # Success
    game.used_words.add(word)
    game.next_turn()
    next_player = game.players[game.current_player_index]
    
    # Update score (1 win point per correct word for now)
    db.update_score(user.id, user.first_name)
    
    await update.message.reply_text(
        f"✅ Good job, {user.first_name}!\n\n"
        f"👉 <b>{next_player['name']}</b>'s Turn.\n"
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
        application.add_handler(CommandHandler("stop", stop_game))
        application.add_handler(CommandHandler("leaderboard", leaderboard))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

        print("Bot is running...")
        application.run_polling()