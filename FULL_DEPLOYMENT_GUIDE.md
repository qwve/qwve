# Instagram Monitor Bot — Full Deployment Guide (Render Free Tier)

This walks through everything, start to finish, for deploying **one client**.
You repeat Part C for every new client — Parts A and B (GitHub repo, Upstash
account) are one-time setup you reuse for all of them.

---

## PART A — One-time setup: put the code on GitHub

Render deploys from a GitHub repo, not from files you upload directly.

1. Go to github.com, create a new **private** repository (e.g. `ig-monitor-bot`).
2. Upload these 6 files into it (drag-and-drop on GitHub's web UI works fine,
   or `git push` if you're comfortable with git):
   - `bot_core.py`
   - `bot_basic.py`
   - `bot_advanced.py`
   - `requirements.txt`
   - `Dockerfile`
   - `.env.example` (just for reference — never upload a real filled-in `.env`)
3. Do **not** upload a real `.env` file to GitHub — it contains secrets.
   Render will let you type env vars into its dashboard instead (Part C).

You now have one repo that serves every client — you don't fork or copy it
per client, you just deploy it multiple times with different settings.

---

## PART B — One-time setup: free persistent storage (Upstash Redis)

This is what stops client data from wiping every time you redeploy.

1. Go to **upstash.com** → sign up (free, no card required).
2. Click **Create Database**.
   - Name: anything, e.g. `ig-monitor`
   - Type: **Redis**
   - Choose the free plan / region closest to you.
3. Once created, open the database → find the **REST API** section.
4. Copy two values — you'll paste these into every client's env vars:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`

You can reuse this **same** Upstash database for all your clients — the bot
automatically keeps each client's data separate internally (keyed by their
Discord user IDs), so one database is enough. Just paste the same two values
into every client's Render service in Part C.

---

## PART C — Per client: create the Discord bot application

Repeat this whole part for each new client.

1. Go to **discord.com/developers/applications** → **New Application**.
   Name it whatever you want the client to see (e.g. "ClientName Monitor").
2. Left sidebar → **Bot** → **Add Bot** (if not already added).
3. On the Bot page:
   - Click **Reset Token** → copy the token somewhere safe. This is your
     `DISCORD_BOT_TOKEN` — treat it like a password, never share it publicly.
   - Turn **off** "Public Bot" if you don't want randoms adding it.
4. Left sidebar → **OAuth2** → **URL Generator**:
   - Scopes: check `bot` and `applications.commands`
   - Bot Permissions: check `View Channels`, `Send Messages`, `Embed Links`,
     `Attach Files` (Attach Files only matters for Advanced tier)
   - Copy the generated URL at the bottom, open it, pick the client's server,
     authorize.
5. Get the client's Discord user ID (their own account, not the bot):
   in Discord, enable **Settings → Advanced → Developer Mode**, then
   right-click their name → **Copy User ID**. This goes in
   `AUTHORIZED_USER_IDS`.

---

## PART D — Per client: deploy on Render

1. Go to **render.com** → sign up / log in → **New +** → **Web Service**.
2. Connect your GitHub account and select the repo from Part A.
3. Settings:
   - **Name:** something identifying this client, e.g. `ig-monitor-clientname`
   - **Instance Type:** Free
   - **Runtime:** Docker (it should auto-detect the `Dockerfile`)
   - **Docker Command / Start Command:** this is the ONE thing that differs
     by tier:
     - Basic client → `python bot_basic.py`
     - Advanced client → `python bot_advanced.py`
4. Scroll to **Environment Variables** and add:

   | Key | Value |
   |---|---|
   | `DISCORD_BOT_TOKEN` | the token from Part C step 3 |
   | `AUTHORIZED_USER_IDS` | the client's Discord user ID from Part C step 5 |
   | `UPSTASH_REDIS_REST_URL` | from Part B |
   | `UPSTASH_REDIS_REST_TOKEN` | from Part B |

   Everything else (`POLL_INTERVAL_SECONDS`, `EMOJI_*`, etc.) is optional —
   leave blank to use sensible defaults, or set them if you want to
   customize this specific client (e.g. their own emoji IDs).

5. Click **Create Web Service**. Render will build and start it — watch the
   **Logs** tab. You should see `Logged in as <botname>` and
   `Persistence backend: Upstash Redis` once it's up.

---

## PART E — Free-tier limitation you should know about

Render's free web services **spin down after ~15 minutes of no incoming web
traffic** and take ~30-60 seconds to wake back up on the next request. The
bot's built-in `/healthz` endpoint exists for exactly this reason, but
Render's free tier doesn't auto-ping it for you — something needs to hit
that URL periodically to keep the bot awake, or it'll go to sleep between
polling cycles and miss checks while asleep.

Simplest fix: use a free uptime-ping service (e.g. UptimeRobot, free plan)
to hit `https://<your-render-service>.onrender.com/healthz` every 5–10
minutes. This is a one-time setup per client (each has a different Render
URL).

---

## PART F — Client-facing setup (inside their Discord server)

Once the bot is online in their server:

1. Client (or you, on their behalf) runs `/panel` — posts the persistent
   control panel with Add / Bulk Add / WatchList / Check / Clear All buttons.
2. Client runs `/config` — picks which channel gets Ban Alerts, Unban Alerts,
   and (Advanced only) Verification Alerts, from a dropdown of their own
   server's channels. Leaving one unset means that alert type only goes to
   their DMs.
3. Client uses the panel buttons to add accounts (single or bulk-paste, one
   username per line) and manage their WatchList.

---

## Quick recap: what's per-client vs. shared

| Thing | Shared across all clients | Per-client |
|---|---|---|
| GitHub repo / code | ✅ | |
| Upstash Redis database | ✅ (one DB, data auto-separated) | |
| Discord bot application/token | | ✅ |
| Render service | | ✅ (one per client) |
| `AUTHORIZED_USER_IDS` | | ✅ |
| Which tier runs (Basic/Advanced) | | ✅ (via Start Command) |
