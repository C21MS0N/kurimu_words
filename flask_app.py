"""
Standalone Flask health endpoint for UptimeRobot monitoring
Runs on port 5000 and completely isolated from the bot
"""
import logging
from flask import Flask

# Suppress werkzeug logs
logging_module = logging
logging_module.getLogger('werkzeug').setLevel(logging_module.CRITICAL)

app = Flask(__name__)
app.logger.disabled = True

@app.route('/')
def health():
    """Health check endpoint for UptimeRobot"""
    return "OK", 200

if __name__ == '__main__':
    print("🌐 Flask health server starting on 0.0.0.0:5000", flush=True)
    app.run(host='0.0.0.0', port=10000, debug=False, use_reloader=False, threaded=True)
import os

if __name__ == "__main__":
    # Render provides a port via environment variables
    port = int(os.environ.get("PORT", 10000))
    # You MUST bind to 0.0.0.0 for Render to see the app
    app.run(host='0.0.0.0', port=port)
    
