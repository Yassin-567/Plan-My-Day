import json
import logging
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor, Json
import requests

log = logging.getLogger("reminder-bot")
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Africa/Cairo").strip()
DEFAULT_REMINDER_TIME = os.environ.get("DEFAULT_REMINDER_TIME", "20:30").strip()
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
TZ = ZoneInfo(TIMEZONE_NAME)
SCHEMA_READY = False


def now_local():
    return datetime.now(TZ)


def parse_hhmm(value):
    return datetime.strptime(value, "%H:%M").time()


def fmt_dt(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ).strftime("%a %d %b, %H:%M")


@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    con = psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor, connect_timeout=5)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    global SCHEMA_READY
    if SCHEMA_READY:
        return
    statements = [
        """CREATE TABLE IF NOT EXISTS reminders (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,
            repeat_daily BOOLEAN NOT NULL DEFAULT FALSE,
            status TEXT NOT NULL DEFAULT 'pending',
            notified BOOLEAN NOT NULL DEFAULT FALSE,
            notification_claimed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            user_id BIGINT PRIMARY KEY,
            default_time TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS user_states (
            user_id BIGINT PRIMARY KEY,
            state JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS processed_updates (
            update_id BIGINT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (due_at) WHERE status='pending' AND notified=FALSE",
        "CREATE INDEX IF NOT EXISTS reminders_user_idx ON reminders (user_id, status, due_at)",
    ]
    with db() as con:
        with con.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
    SCHEMA_READY = True


def api(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload or {}, timeout=6,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {result}")
    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api("sendMessage", payload)


def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    payload["reply_markup"] = {"inline_keyboard": keyboard or []}
    return api("editMessageText", payload)


def answer_callback(callback_id):
    api("answerCallbackQuery", {"callback_query_id": callback_id})


def btn(text, data):
    return {"text": text, "callback_data": data}


def action_keyboard(rid):
    return [[btn("Done", f"action:done:{rid}"), btn("Remind me later", f"action:later:{rid}"), btn("Delete", f"action:delete:{rid}")]]


def time_keyboard(prefix="time"):
    values = ["08:00", "12:00", "15:00", "18:00", "20:30"]
    rows = [[btn(v, f"{prefix}:{v}") for v in values[i:i+3]] for i in range(0, len(values), 3)]
    rows += [[btn("Custom time", "custom_time")], [btn("Cancel", "cancel_flow")]]
    return rows


def calendar_keyboard(year, month, prefix="day"):
    first = date(year, month, 1)
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    days = (next_month - timedelta(days=1)).day
    rows = [[btn(x, "noop") for x in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]]]
    week = [btn(" ", "noop") for _ in range(first.weekday())]
    for d in range(1, days + 1):
        selected = date(year, month, d)
        week.append(btn(str(d), f"{prefix}:{selected.isoformat()}") if selected >= now_local().date() else btn("·", "noop"))
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        week += [btn(" ", "noop") for _ in range(7 - len(week))]
        rows.append(week)
    prev_month = first - timedelta(days=1)
    rows.append([btn("‹", f"month:{prev_month.year}:{prev_month.month}"), btn(first.strftime("%B %Y"), "noop"), btn("›", f"month:{next_month.year}:{next_month.month}")])
    rows.append([btn("Cancel", "cancel_flow")])
    return rows


def owner_allowed(user_id):
    if ALLOWED_USER_ID:
        return str(user_id) == ALLOWED_USER_ID
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='owner_id' FOR UPDATE")
            row = cur.fetchone()
            if row:
                return int(row["value"]) == int(user_id)
            cur.execute("INSERT INTO app_config(key,value) VALUES('owner_id',%s) ON CONFLICT DO NOTHING", (str(user_id),))
            cur.execute("SELECT value FROM app_config WHERE key='owner_id'")
            return int(cur.fetchone()["value"]) == int(user_id)


def get_state(user_id):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT state FROM user_states WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    return row["state"] if row else {}


def set_state(user_id, state):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("""INSERT INTO user_states(user_id,state,updated_at) VALUES(%s,%s,NOW())
                ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state, updated_at=NOW()""", (user_id, Json(state)))


def clear_state(user_id):
    set_state(user_id, {})


def get_default_time(user_id):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT default_time FROM settings WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    return row["default_time"] if row else DEFAULT_REMINDER_TIME


def set_default_time(user_id, value):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("""INSERT INTO settings(user_id,default_time) VALUES(%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET default_time=EXCLUDED.default_time""", (user_id, value))


def next_default_due(user_id, from_dt=None):
    current = from_dt or now_local()
    candidate = datetime.combine(current.date(), parse_hhmm(get_default_time(user_id)), TZ)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def add_reminder(user_id, text, due, repeat_daily=False):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("""INSERT INTO reminders(user_id,text,due_at,repeat_daily,status,notified)
                VALUES(%s,%s,%s,%s,'pending',FALSE) RETURNING id""", (user_id, text, due, repeat_daily))
            return cur.fetchone()["id"]


def get_reminder(rid, user_id):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT * FROM reminders WHERE id=%s AND user_id=%s AND status='pending'", (rid, user_id))
            return cur.fetchone()


def update_reminder(rid, user_id, **values):
    allowed = {"status", "due_at", "repeat_daily", "notified"}
    values = {k: v for k, v in values.items() if k in allowed}
    if not values:
        return
    clause = ", ".join(f"{key}=%s" for key in values)
    with db() as con:
        with con.cursor() as cur:
            cur.execute(f"UPDATE reminders SET {clause}, notification_claimed_at=NULL WHERE id=%s AND user_id=%s", (*values.values(), rid, user_id))


def list_reminders(user_id, limit=20):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT * FROM reminders WHERE user_id=%s AND status='pending' ORDER BY due_at ASC LIMIT %s", (user_id, limit))
            return cur.fetchall()


def finish_new_reminder(user_id, state, chosen_time, chat_id, message_id=None):
    draft, chosen_date = state.get("draft"), state.get("chosen_date")
    if not draft or not chosen_date:
        clear_state(user_id)
        text = "The reminder details expired. Please send the task again."
    else:
        due = datetime.combine(date.fromisoformat(chosen_date), chosen_time, TZ)
        if due <= now_local():
            text = "That date and time has passed. Choose another date:"
            state["state"] = "choose_date"
            set_state(user_id, state)
            today = now_local().date()
            keyboard = calendar_keyboard(today.year, today.month)
            if message_id:
                edit_message(chat_id, message_id, text, keyboard)
            else:
                send_message(chat_id, text, keyboard)
            return
        else:
            rid = add_reminder(user_id, draft, due)
            clear_state(user_id)
            text = f"Reminder #{rid} saved for {fmt_dt(due)}."
    if message_id:
        edit_message(chat_id, message_id, text)
    else:
        send_message(chat_id, text)


def process_message(message):
    user = message.get("from") or {}
    user_id = user.get("id")
    chat_id = (message.get("chat") or {}).get("id")
    if not user_id or not chat_id:
        return
    if not owner_allowed(user_id):
        send_message(chat_id, "This is a private reminder bot. You are not authorized to use it.")
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    state = get_state(user_id)
    if text.startswith("/"):
        command = text.split()[0].split("@")[0].lower()
        if command == "/start":
            send_message(chat_id, "Welcome! Send me a task and I'll help you remember it.\n\nCommands:\n/list - pending reminders\n/today - due or overdue today\n/settings - default reminder time\n/cancel - cancel the current action")
            return
        if command == "/cancel":
            clear_state(user_id)
            send_message(chat_id, "Cancelled. Send me a new task whenever you like.")
            return
        if command in ("/list", "/today"):
            rows = list_reminders(user_id, 100 if command == "/today" else 20)
            today = now_local().date()
            if command == "/today":
                rows = [r for r in rows if r["due_at"].astimezone(TZ).date() <= today]
            if not rows:
                send_message(chat_id, "No reminders due or overdue today." if command == "/today" else "You have no pending reminders.")
                return
            send_message(chat_id, f"You have {len(rows)} pending reminder(s):")
            for row in rows:
                due = row["due_at"].astimezone(TZ)
                kind = "Daily" if row["repeat_daily"] else "One-time"
                label = ("OVERDUE" if due.date() < today or due < now_local() else "DUE TODAY") if command == "/today" else kind
                send_message(chat_id, f"{label} · #{row['id']} · {fmt_dt(due)}\n{row['text']}", action_keyboard(row["id"]))
            return
        if command == "/settings":
            send_message(chat_id, f"Timezone: {TIMEZONE_NAME}\nDefault daily reminder: {get_default_time(user_id)}", [[btn("Change default time", "settings_change_time")]])
            return
    if state.get("state") in ("custom_time", "custom_snooze_time"):
        try:
            chosen_time = parse_hhmm(text)
        except ValueError:
            send_message(chat_id, "Please send a time in 24-hour format, for example 14:30.")
            return
        if state.get("state") == "custom_snooze_time":
            finish_snooze(user_id, state, chosen_time, chat_id)
        else:
            finish_new_reminder(user_id, state, chosen_time, chat_id)
        return
    if state.get("state") == "changing_default":
        try:
            chosen_time = parse_hhmm(text).strftime("%H:%M")
        except ValueError:
            send_message(chat_id, "Please send a time in 24-hour format, for example 20:30.")
            return
        set_default_time(user_id, chosen_time)
        clear_state(user_id)
        send_message(chat_id, f"Default reminder time updated to {chosen_time}.")
        return
    set_state(user_id, {"state": "choose_type", "draft": text})
    send_message(chat_id, f"Saved your note:\n\n{text}\n\nWhen should I remind you?", [[btn("Choose date", "choose_date")], [btn("Every day", "make_daily")], [btn("Cancel", "cancel_flow")]])


def finish_snooze(user_id, state, chosen_time, chat_id, message_id=None):
    rid, chosen_date = state.get("snooze_id"), state.get("chosen_date")
    if not rid or not chosen_date:
        clear_state(user_id)
        result = "The snooze details expired. Please try again."
    else:
        due = datetime.combine(date.fromisoformat(chosen_date), chosen_time, TZ)
        if due <= now_local():
            result = "That date and time has passed. Choose another date:"
            state["state"] = "snooze_date"
            set_state(user_id, state)
            today = now_local().date()
            keyboard = calendar_keyboard(today.year, today.month)
            if message_id:
                edit_message(chat_id, message_id, result, keyboard)
            else:
                send_message(chat_id, result, keyboard)
            return
        else:
            update_reminder(rid, user_id, due_at=due, repeat_daily=False, notified=False)
            clear_state(user_id)
            result = f"Rescheduled for {fmt_dt(due)}."
    if message_id:
        edit_message(chat_id, message_id, result)
    else:
        send_message(chat_id, result)


def process_callback(callback):
    user = callback.get("from") or {}
    user_id = user.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    callback_id = callback.get("id")
    if not user_id or not chat_id:
        return
    if not owner_allowed(user_id):
        answer_callback(callback_id)
        return
    data = callback.get("data", "")
    answer_callback(callback_id)
    state = get_state(user_id)
    if data == "noop":
        return
    if data == "cancel_flow":
        clear_state(user_id)
        edit_message(chat_id, message_id, "Cancelled.")
        return
    if data == "choose_date":
        today = now_local().date()
        state["state"] = "choose_date"
        set_state(user_id, state)
        edit_message(chat_id, message_id, "Choose the reminder date:", calendar_keyboard(today.year, today.month))
        return
    if data.startswith("month:"):
        _, y, m = data.split(":")
        edit_message(chat_id, message_id, "Choose the date:", calendar_keyboard(int(y), int(m)))
        return
    if data.startswith("day:"):
        chosen = date.fromisoformat(data.split(":", 1)[1])
        state["chosen_date"] = chosen.isoformat()
        state["state"] = "choose_time"
        set_state(user_id, state)
        edit_message(chat_id, message_id, f"Date selected: {chosen.strftime('%A %d %B %Y')}\nChoose a time:", time_keyboard())
        return
    if data == "make_daily":
        draft = state.get("draft")
        if not draft:
            edit_message(chat_id, message_id, "Please send the task again.")
            return
        rid = add_reminder(user_id, draft, next_default_due(user_id), True)
        clear_state(user_id)
        edit_message(chat_id, message_id, f"Daily reminder #{rid} saved. I'll remind you every day at {get_default_time(user_id)} until you mark it Done or Delete it.")
        return
    if data.startswith("time:"):
        chosen_time = parse_hhmm(data.split(":", 1)[1])
        if state.get("snooze_id"):
            finish_snooze(user_id, state, chosen_time, chat_id, message_id)
        else:
            finish_new_reminder(user_id, state, chosen_time, chat_id, message_id)
        return
    if data == "custom_time":
        state["state"] = "custom_snooze_time" if state.get("snooze_id") else "custom_time"
        set_state(user_id, state)
        edit_message(chat_id, message_id, "Send the time in 24-hour format, for example 14:30.")
        return
    if data == "settings_change_time":
        set_state(user_id, {"state": "changing_default"})
        edit_message(chat_id, message_id, "Send your new default time in 24-hour format, for example 20:30.")
        return
    if data.startswith("action:"):
        _, action, rid_text = data.split(":")
        rid = int(rid_text)
        row = get_reminder(rid, user_id)
        if not row:
            edit_message(chat_id, message_id, "This reminder is no longer pending.")
        elif action == "done":
            update_reminder(rid, user_id, status="completed")
            edit_message(chat_id, message_id, "Marked as done. Nice work!")
        elif action == "delete":
            update_reminder(rid, user_id, status="deleted")
            edit_message(chat_id, message_id, "Reminder deleted.")
        elif action == "later":
            set_state(user_id, {"snooze_id": rid})
            edit_message(chat_id, message_id, "When should I remind you again?", [[btn("In 1 hour", "snooze:1h"), btn("Tomorrow", "snooze:tomorrow")], [btn("Next week", "snooze:week"), btn("Choose date", "snooze:custom")], [btn("Every day at default time", "snooze:daily")]])
        return
    if data == "snooze:custom":
        today = now_local().date()
        state["state"] = "snooze_date"
        set_state(user_id, state)
        edit_message(chat_id, message_id, "Choose the new reminder date:", calendar_keyboard(today.year, today.month))
        return
    if data.startswith("snooze:"):
        choice = data.split(":", 1)[1]
        rid = state.get("snooze_id")
        if not rid or not get_reminder(rid, user_id):
            edit_message(chat_id, message_id, "Reminder not found.")
            return
        now = now_local()
        if choice == "1h":
            due, daily = now + timedelta(hours=1), False
        elif choice == "tomorrow":
            due, daily = datetime.combine(now.date() + timedelta(days=1), parse_hhmm(get_default_time(user_id)), TZ), False
        elif choice == "week":
            due, daily = now + timedelta(days=7), False
        elif choice == "daily":
            due, daily = next_default_due(user_id), True
        else:
            return
        update_reminder(rid, user_id, due_at=due, repeat_daily=daily, notified=False)
        clear_state(user_id)
        edit_message(chat_id, message_id, f"Okay. I'll remind you {'every day at ' + get_default_time(user_id) if daily else 'at ' + fmt_dt(due)}.")
        return


def handle_update(update):
    update_id = update.get("update_id")
    if update_id is not None:
        with db() as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1 FROM processed_updates WHERE update_id=%s", (update_id,))
                if cur.fetchone():
                    return
    # Only mark an update processed after its handler succeeds, so Telegram retries
    # can recover from transient database or Telegram API failures.
    if "message" in update:
        process_message(update["message"])
    elif "callback_query" in update:
        process_callback(update["callback_query"])
    if update_id is not None:
        with db() as con:
            with con.cursor() as cur:
                cur.execute("INSERT INTO processed_updates(update_id) VALUES(%s) ON CONFLICT DO NOTHING", (update_id,))


def send_due_reminders(batch_size=2):
    """Claim due reminders atomically; stale claims are retried after five minutes."""
    now = now_local()
    claimed = []
    with db() as con:
        with con.cursor() as cur:
            cur.execute("""SELECT * FROM reminders
                WHERE status='pending' AND notified=FALSE AND due_at <= %s
                  AND (notification_claimed_at IS NULL OR notification_claimed_at < %s)
                ORDER BY due_at ASC LIMIT %s FOR UPDATE SKIP LOCKED""",
                (now, now - timedelta(minutes=5), batch_size))
            rows = cur.fetchall()
            for row in rows:
                cur.execute("UPDATE reminders SET notification_claimed_at=%s WHERE id=%s", (now, row["id"]))
                claimed.append(row)
    sent = 0
    for row in claimed:
        rid, user_id = row["id"], row["user_id"]
        due = row["due_at"].astimezone(TZ)
        try:
            send_message(user_id, f"⏰ REMINDER\n\n{row['text']}\n\nScheduled: {fmt_dt(due)}", action_keyboard(rid))
            with db() as con:
                with con.cursor() as cur:
                    if row["repeat_daily"]:
                        next_due = datetime.combine(now.date() + timedelta(days=1), parse_hhmm(get_default_time(user_id)), TZ)
                        cur.execute("UPDATE reminders SET due_at=%s, notified=FALSE, notification_claimed_at=NULL WHERE id=%s", (next_due, rid))
                    else:
                        cur.execute("UPDATE reminders SET notified=TRUE, notification_claimed_at=NULL WHERE id=%s", (rid,))
            sent += 1
        except Exception:
            log.exception("Could not send reminder id=%s", rid)
            with db() as con:
                with con.cursor() as cur:
                    cur.execute("UPDATE reminders SET notification_claimed_at=NULL WHERE id=%s", (rid,))
    return {"claimed": len(claimed), "sent": sent}
