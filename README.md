# Personal Telegram Reminder Bot — Vercel + Neon

This version is designed for Vercel Functions and Neon PostgreSQL. It uses one FastAPI entrypoint for both HTTP endpoints, so Vercel does not have to choose between multiple standalone Python handlers. It uses a Telegram webhook (not long polling), stores conversation state and reminders in PostgreSQL, and exposes a protected endpoint for a scheduler to check due reminders.

## Included features

- Send a text note, then choose a date/time or repeat daily.
- Default daily time: 20:30 in `Africa/Cairo`.
- `/start`, `/list`, `/today`, `/settings`, `/cancel`.
- Reminder buttons: Done, Remind me later, Delete.
- Snooze: 1 hour, tomorrow, next week, custom date/time, or daily at default time.
- One-time reminders that have been delivered remain pending/overdue until Done or Delete, without being sent again.
- Daily reminders repeat until Done or Delete.
- Private owner claim on first `/start`, or set `ALLOWED_USER_ID` to lock it to your Telegram ID.

## Architecture

- `api/webhook.py`: receives Telegram updates at `/api/webhook`.
- `api/cron.py`: protected endpoint at `/api/cron` that sends due reminders.
- `reminder_app.py`: bot logic and PostgreSQL operations.
- Neon PostgreSQL: persistent data; no SQLite or local filesystem database.

## 1. Create the Telegram bot

In Telegram, open `@BotFather`, send `/newbot`, and copy the token. Keep it private.

## 2. Create a Neon database

1. Create a Neon project.
2. In Neon, choose **Connect** and copy the pooled connection string (the hostname normally contains `-pooler`).
3. Keep this value for the Vercel `DATABASE_URL` environment variable. The application creates its tables automatically on the first webhook/cron request.

## 3. Deploy to Vercel

1. Put this folder in a GitHub repository.
2. Import the repository into Vercel.
3. Add these Environment Variables in Vercel (Production):
   - `TELEGRAM_BOT_TOKEN`
   - `DATABASE_URL`
   - `WEBHOOK_SECRET`
   - `CRON_SECRET`
   - `TIMEZONE` = `Africa/Cairo`
   - `DEFAULT_REMINDER_TIME` = `20:30`
   - Optional: `ALLOWED_USER_ID` = your Telegram numeric user ID
4. Deploy. Your webhook URL will be `https://YOUR-PROJECT.vercel.app/api/webhook`.

Generate two different long random secrets for `WEBHOOK_SECRET` and `CRON_SECRET`. Do not commit `.env` or secrets to GitHub.

## 4. Register the Telegram webhook

On your Windows computer:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN` and `WEBHOOK_SECRET` in `.env`. Then run:

```powershell
python set_webhook.py https://YOUR-PROJECT.vercel.app
```

Telegram should return `{"ok": true, ...}`. You only need to register it again if the webhook URL or secret changes.

## 5. Schedule reminder checks

For minute-by-minute checks on the free Vercel Hobby plan, use an external scheduler such as [cron-job.org](https://cron-job.org/). Its official FAQ says it is free and supports up to 60 executions per hour (once per minute). Vercel's traditional Hobby Cron is not suitable for this frequency.

Create one job in cron-job.org:

- URL: `https://YOUR-PROJECT.vercel.app/api/cron`
- Method: `GET`
- Schedule: every 1 minute
- Custom request header: `Authorization: Bearer YOUR_CRON_SECRET`

The endpoint is safe to call repeatedly. It claims due reminders in PostgreSQL before sending and retries stale claims after five minutes. It processes up to two reminders per run to stay within external scheduler request time limits. For this personal bot, checks should normally reach within about one minute of the due time, subject to scheduler and Vercel response time.

Vercel announced **Vercel Schedules** in public beta on 5 November 2026, including support for recurring schedules on all plans. The feature is new, and its configuration/limits may evolve, so this starter uses cron-job.org as the known, simple minute-level trigger. You can replace it with Vercel Schedules later if your project has access to the required schedule feature.

## 6. Test

1. Send `/start` to your bot.
2. Create a one-time reminder for 2–3 minutes ahead.
3. Confirm the scheduled trigger calls `/api/cron` and the bot sends it.
4. Test Done, Remind me later, and Delete.
5. Create a daily reminder and confirm the next occurrence is scheduled.
6. Restart nothing: data should remain in Neon across deployments.

## Notes and limitations

- This is a serverless webhook design; do not run `bot.py` or a permanent polling process on Vercel.
- Delivery is best-effort with retry after a stale claim. As with most webhook/scheduler systems, a rare crash after Telegram accepts a message but before the database records success could result in a duplicate on retry.
- cron-job.org documents a 30-second timeout for each request. This endpoint limits each run to two sends; if several reminders are overdue, remaining reminders are picked up on the next minute's run.
- Keep the scheduler's secret private. Anyone who knows the cron URL and secret could trigger reminder checks.
- Check Vercel and Neon usage limits before relying on the free tiers long term.
\n\n## Build-error fix\n\nThis revision replaces the two separate `api/cron.py` and `api/webhook.py` handlers with one FastAPI app in `api/index.py`. The root `pyproject.toml` declares `api.index:app` as the Vercel entrypoint. Both public paths remain available: `/api/webhook` and `/api/cron`.\n