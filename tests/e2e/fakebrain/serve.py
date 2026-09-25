"""Stand-in brain for the end-to-end test: answers /health on loopback."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("FAKEBRAIN_PORT", "8799"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "user": os.getuid()}).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
