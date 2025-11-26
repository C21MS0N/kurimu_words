"""
Main entry point - runs Flask and Telegram bot in separate processes
This wrapper ensures both run independently without conflicts
"""
import subprocess
import sys
import time
import atexit

flask_process = None
bot_process = None

def cleanup():
    """Clean up processes on exit"""
    global flask_process, bot_process
    print("\n🛑 Cleaning up processes...", flush=True)
    if flask_process:
        try:
            flask_process.terminate()
            flask_process.wait(timeout=5)
        except:
            flask_process.kill()
    if bot_process:
        try:
            bot_process.terminate()
            bot_process.wait(timeout=5)
        except:
            bot_process.kill()
    print("✅ Cleanup complete", flush=True)

atexit.register(cleanup)

def start_flask():
    """Start Flask in subprocess"""
    global flask_process
    print("🌐 Starting Flask server...", flush=True)
    flask_process = subprocess.Popen(
        [sys.executable, "flask_app.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    print(f"✅ Flask started (PID: {flask_process.pid})", flush=True)

def start_bot():
    """Start Telegram bot in subprocess with auto-restart"""
    global bot_process
    print("🎮 Starting Telegram bot...", flush=True)
    bot_process = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    print(f"✅ Bot started (PID: {bot_process.pid})", flush=True)

def monitor_processes():
    """Monitor and restart processes if they die"""
    while True:
        # Check Flask
        if flask_process and flask_process.poll() is not None:
            print("⚠️  Flask died! Restarting...", flush=True)
            start_flask()
        
        # Check Bot
        if bot_process and bot_process.poll() is not None:
            print("⚠️  Bot died! Restarting...", flush=True)
            start_bot()
        
        time.sleep(10)

if __name__ == '__main__':
    print("🚀 LAUNCHING UNBREAKABLE BOT + FLASK", flush=True)
    
    # Start both processes
    start_flask()
    time.sleep(1)
    start_bot()
    
    print("✅ Both services started - monitoring for failures...", flush=True)
    print("🟢 APP ONLINE - HEALTH ENDPOINT ACTIVE & BOT RUNNING", flush=True)
    
    # Monitor and restart if needed
    try:
        monitor_processes()
    except KeyboardInterrupt:
        print("\n✋ Manual stop detected", flush=True)
        cleanup()
        sys.exit(0)
