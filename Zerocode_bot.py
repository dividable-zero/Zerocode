import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

# 1. Create a tiny web server so Render stays happy
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ZeroCode bot is alive!")
    
    # Suppress console log spam from ping requests
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# 2. Start the web server in a background thread when the script boots
threading.Thread(target=run_web_server, daemon=True).start()
