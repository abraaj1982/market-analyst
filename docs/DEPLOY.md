# Deploying online

The app is a normal Dockerized FastAPI service (dashboard + REST API +
background scheduler), so it runs on any platform that builds a `Dockerfile`
and gives you a persistent volume. These steps use **Railway** because it
needs the fewest manual steps and its free/hobby tier includes a volume.

## 1. Push the repo to GitHub

Already done if you're reading this from `github.com/abraaj1982/market-analyst`.

## 2. Create the service

1. Go to [railway.app](https://railway.app) and sign in with GitHub.
2. **New Project → Deploy from GitHub repo** → pick `market-analyst`.
   Railway detects the `Dockerfile` automatically — no build config needed.
3. Wait for the first deploy. It will crash-loop until step 3 is done
   (no persistent disk yet), that's expected.

## 3. Add a persistent volume

The SQLite database must not live on the container's ephemeral disk, or
every redeploy wipes it.

1. Open the service → **Settings → Volumes → New Volume**.
2. Mount path: `/app/data`
3. Redeploy.

## 4. Set environment variables

Service → **Variables**:

| Variable | Required | Notes |
|---|---|---|
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | **Strongly recommended** | Without these the dashboard and API are open to anyone with the URL |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional | For alerts — see `.env.example` for how to get them |
| `FRED_API_KEY` | Optional | Raises macro-data rate limits |

`ANALYST_DATABASE_URL` is already set inside the `Dockerfile` to point at the
mounted volume — leave it alone.

## 5. Get the URL

Service → **Settings → Networking → Generate Domain**. That URL is the
dashboard; the API lives under the same host at `/api/...`
(`/api/docs` for the interactive schema, `/api/health` for an unauthenticated
health check).

## 6. Load the MSX seed companies (one-time)

Railway's **Shell** tab (or `railway run`) on the deployed service:

```bash
python scripts/import_companies.py seeds/msx_companies.csv
```

## What runs where

- The web dashboard and REST API run in the container's `analyst serve`
  process, bound to Railway's `$PORT`.
- The scheduler (periodic re-analysis) runs inside the same process — no
  separate worker needed, since Railway keeps the service alive continuously
  rather than putting it to sleep between requests.
- Everything in `data/` (SQLite database) survives redeploys via the volume;
  everything else is rebuilt from the repo on each deploy.

## Alternatives

Render and Fly.io both work the same way (Dockerfile + a persistent
disk/volume + the same environment variables) if you prefer either of those
instead.
