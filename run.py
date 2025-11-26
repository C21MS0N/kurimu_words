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
            try:
                flask_process.kill()
            except:
                pass
    if bot_process:
        try:
            bot_process.terminate()
            bot_process.wait(timeout=5)
        except:
            try:
                bot_process.kill()
            except:
                pass
    print("✅ Cleanup complete", flush=True)

atexit.register(cleanup)

def start_flask():
    """Start Flask in subprocess - output goes directly to console"""
    global flask_process
    print("🌐 Starting Flask server...", flush=True)
    flask_process = subprocess.Popen(
        [sys.executable, "flask_app.py"],
        stdout=None,  # Inherit parent's stdout
        stderr=None,  # Inherit parent's stderr
    )
    print(f"✅ Flask started (PID: {flask_process.pid})", flush=True)
    return flask_process

def start_bot():
    """Start Telegram bot in subprocess - output goes directly to console"""
    global bot_process
    print("🎮 Starting Telegram bot...", flush=True)
    bot_process = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=None,  # Inherit parent's stdout
        stderr=None,  # Inherit parent's stderr
    )
    print(f"✅ Bot started (PID: {bot_process.pid})", flush=True)
    return bot_process

def monitor_processes():
    """Monitor and restart processes if they die"""
    last_flask_check = 0
    last_bot_check = 0
    
    while True:
        now = time.time()
        
        # Check Flask every 5 seconds
        if now - last_flask_check >= 5:
            if flask_process and flask_process.poll() is not None:
                exit_code = flask_process.returncode
                print(f"⚠️  Flask died (exit code: {exit_code})! Restarting...", flush=True)
                start_flask()
            last_flask_check = now
        
        # Check Bot every 5 seconds
        if now - last_bot_check >= 5:
            if bot_process and bot_process.poll() is not None:
                exit_code = bot_process.returncode
                print(f"⚠️  Bot died (exit code: {exit_code})! Restarting...", flush=True)
                start_bot()
            last_bot_check = now
        
        time.sleep(2)

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
    except Exception as e:
        print(f"❌ Monitor error: {e}", flush=True)
        cleanup()
        sys.exit(1)
