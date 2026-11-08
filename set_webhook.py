"""Run locally once after deploying: python set_webhook.py https://your-project.vercel.app"""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
secret = os.environ.get("WEBHOOK_SECRET", "").strip()
if len(sys.argv) != 2 or not token or not secret:
    raise SystemExit("Usage: python set_webhook.py https://your-project.vercel.app (with TELEGRAM_BOT_TOKEN and WEBHOOK_SECRET in .env)")
base_url = sys.argv[1].rstrip("/")
response = requests.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={"url": f"{base_url}/api/webhook", "secret_token": secret, "allowed_updates": ["message", "callback_query"], "drop_pending_updates": False},
    timeout=20,
)
response.raise_for_status()
print(response.json())
