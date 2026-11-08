import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

from reminder_app import handle_update, init_db

log = logging.getLogger(__name__)

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        expected = os.environ.get("WEBHOOK_SECRET", "")
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                self.send_response(413)
                self.end_headers()
                return
            update = json.loads(self.rfile.read(length) or b"{}")
            init_db()
            handle_update(update)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception:
            log.exception("Webhook processing failed")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal error")
