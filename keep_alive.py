import os
import requests
import time

# Get the Replit domain for this project
# Replit exposes the domain via REPLIT_DEV_DOMAIN or REPL_ID.repl.co
REPLIT_DOMAIN = os.environ.get("REPLIT_DEV_DOMAIN")
if not REPLIT_DOMAIN:
    REPL_ID = os.environ.get("REPL_ID")
    if REPL_ID:
        REPLIT_DOMAIN = f"{REPL_ID}.repl.co"

URL = f"https://{REPLIT_DOMAIN}/" if REPLIT_DOMAIN else None

def keep_alive():
    if not URL:
        print("❌ Could not determine health URL. Make sure project is published or running.")
        return
    
    print(f"📡 Starting Keep-Alive ping for: {URL}")
    while True:
        try:
            # Ping the Flask health endpoint (usually on port 5000)
            # Replit's proxy handles the routing
            response = requests.get(URL, timeout=10)
            print(f"✅ Ping successful: {response.status_code}")
        except Exception as e:
            print(f"⚠️ Ping failed: {e}")
        
        # Wait 5 minutes (300 seconds) between pings
        time.sleep(300)

if __name__ == "__main__":
    keep_alive()
