import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running with NEPTUN alerts and private access!")

class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = ReusableHTTPServer(('0.0.0.0', port), DummyHandler)
        print(f"Dummy web server started on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"Dummy web server skipped (port {port} already in use): {e}")

def start_server_thread():
    threading.Thread(target=run_dummy_server, daemon=True).start()
