import hmac
import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from reminder_app import handle_update, init_db, send_due_reminders

log = logging.getLogger("reminder-bot-api")
app = FastAPI(title="Personal Telegram Reminder Bot")


def _authorized(expected: str, supplied: str) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


async def _webhook(request: Request):
    expected = os.environ.get("WEBHOOK_SECRET", "").strip()
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").strip()
    if not _authorized(expected, supplied):
        return PlainTextResponse("Forbidden", status_code=403)

    try:
        raw = await request.body()
        if len(raw) > 1_000_000:
            return PlainTextResponse("Payload too large", status_code=413)
        update = json.loads(raw or b"{}")
        init_db()
        handle_update(update)
        return PlainTextResponse("OK", status_code=200)
    except Exception:
        log.exception("Webhook processing failed")
        return PlainTextResponse("Internal error", status_code=500)


async def _cron(request: Request):
    expected = os.environ.get("CRON_SECRET", "").strip()
    supplied = request.headers.get("Authorization", "").strip()
    if not _authorized(f"Bearer {expected}", supplied):
        return PlainTextResponse("Forbidden", status_code=403)

    try:
        init_db()
        result = send_due_reminders()
        return JSONResponse({"ok": True, **result})
    except Exception:
        log.exception("Reminder cron failed")
        return JSONResponse({"ok": False, "error": "Internal error"}, status_code=500)


# Vercel normally forwards the /api prefix to this app. The short aliases
# also support deployments where the function mount strips that prefix.
app.add_api_route("/api/webhook", _webhook, methods=["POST"])
app.add_api_route("/webhook", _webhook, methods=["POST"])
app.add_api_route("/api/cron", _cron, methods=["GET"])
app.add_api_route("/cron", _cron, methods=["GET"])


@app.get("/")
async def health():
    return {"ok": True, "service": "personal-telegram-reminder-bot"}
