import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

from reminder_app import init_db, send_due_reminders

log = logging.getLogger(__name__)

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ.get("CRON_SECRET", "")
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return
        try:
            init_db()
            result = send_due_reminders()
            body = json.dumps({"ok": True, **result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            log.exception("Reminder cron failed")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal error")
