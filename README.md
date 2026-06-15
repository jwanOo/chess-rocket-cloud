# Chess Rocket — Cloud Deployment

This is the deploy-only copy of [chess_rocket](../chess_rocket/) configured for [Render.com](https://render.com) container hosting.

**Local development still happens in `~/Desktop/chess_rocket/`. This folder exists solely to deploy a 1:1 copy of that app to the cloud so it's reachable from your iPhone as a PWA.**

---

## What's deployed

The full local app, unchanged:
- `dashboard_server.py` — Python HTTP server (stateful per-session `GameManager`)
- `engine.py` — Stockfish UCI wrapper (real Stockfish binary, not the JS port)
- `sap_coach.py` — SAP AI Core (Claude 4.7 Opus) coaching
- `tactics_trainer.py` — puzzle picker + SRS over `puzzles/*.json`
- `motif_detector.py`, `openings.py`, `srs.py`, `models.py`
- All three pages: `dashboard.html` (play), `setup.html`, `tactics.html`

iOS PWA polish added in this copy only:
- `setup.html` and `tactics.html` got the same `apple-mobile-web-app-*` meta tags as `dashboard.html`
- `tactics.html` cleared its hard-coded Fly.io `cr-backend-url` → empty (same-origin works for Render)
- All three pages link to `/manifest.webmanifest` and load icons + apple-touch-icon

What's NOT in this copy (bloat removed):
- `data/lichess_db_puzzle.csv.zst` (286 MB) — Tactics still works via the curated `puzzles/*.json` files
- `.venv/`, `mcp-server/`, `tests/`, `references/`, `assets/`, build/import scripts
- All Vercel/Fly cruft (`vercel.json`, `fly.toml`, `api/`)

---

## Deploy to Render (one-time setup, ~5 min)

### 1. Push this folder to a fresh GitHub repo
```bash
cd ~/Desktop/chess-rocket-cloud
git init -b main
git add .
git commit -m "initial: chess-rocket cloud deploy for Render"
# Create a NEW empty repo on GitHub (e.g. chess-rocket-cloud), then:
git remote add origin git@github.com:<YOUR_USER>/chess-rocket-cloud.git
git push -u origin main
```

### 2. Connect Render to that repo
1. Go to https://dashboard.render.com → **New** → **Blueprint**
2. Connect your GitHub account, pick the `chess-rocket-cloud` repo
3. Render reads `render.yaml` and asks for the secret env vars (the ones marked `sync: false`)

### 3. Paste your SAP AI Core secrets
Render will prompt for these four secrets (the rest are auto-set by `render.yaml`):

| Key | Value |
|---|---|
| `AICORE_ORCH_AUTH_URL` | (from your SAP BTP service key) |
| `AICORE_ORCH_CLIENT_ID` | (from your SAP BTP service key) |
| `AICORE_ORCH_CLIENT_SECRET` | (from your SAP BTP service key) |
| `AICORE_ORCH_BASE_URL` | (from your SAP BTP service key) |

You can copy these from `~/Documents/Repos/sap-architecture-validator/.env` or wherever you keep them locally.

The other three (`AICORE_ORCH_RESOURCE_GROUP`, `AICORE_DIRECT_DEPLOYMENT_ID`, `AICORE_DIRECT_MODEL_NAME`) are pre-filled with your CPI / Claude 4.7 Opus values.

### 4. Wait for build (~3–4 min)
Render builds the Docker image, installs Stockfish via `apt-get`, runs `dashboard_server.py`. When the health check on `/healthz` passes, the service goes live at:

```
https://chess-rocket-backend.onrender.com    (or similar)
```

### 5. Install on iPhone
1. Open the Render URL in **iOS Safari**
2. Tap the share button → **Add to Home Screen**
3. Confirm — the app installs as "Chess Rocket" with a chess-knight icon
4. Launch from the home screen → opens full-screen, no Safari chrome

---

## Free tier reality check

- **Cold start:** the service sleeps after 15 min idle. First request after that takes ~30–60 s while the container wakes.
- **Workaround:** Set up a free [cron-job.org](https://cron-job.org) ping to `https://<your-url>/healthz` every 10 minutes to keep it warm.
- **RAM:** 512 MB is plenty for Stockfish + Python + Claude HTTPS calls.
- **Auto-deploy:** every `git push` to `main` redeploys (set in `render.yaml`).

To upgrade to always-on: change `plan: free` → `plan: starter` in `render.yaml` ($7/mo).

---

## Updating the deployed app

Anytime you want to push changes from your local app to the cloud copy:

```bash
# Sync changes from local → cloud copy (be selective; don't sync runtime data/)
cp ~/Desktop/chess_rocket/scripts/sap_coach.py ~/Desktop/chess-rocket-cloud/scripts/
cp ~/Desktop/chess_rocket/scripts/dashboard_server.py ~/Desktop/chess-rocket-cloud/scripts/
# … etc

cd ~/Desktop/chess-rocket-cloud
git add .
git commit -m "sync from local"
git push
# Render auto-deploys.
```

NOTE: keep the iOS PWA changes in `setup.html` / `tactics.html` (the meta tags) when you sync, or they'll get overwritten by the local versions that don't have them.

---

## Troubleshooting

**Build fails with "stockfish: command not found"**
The Dockerfile installs it via `apt-get install stockfish`. Check Render build logs — if Debian's repo is temporarily down, retry the deploy.

**SAP AI Core coach silently falls back to heuristics**
The env var names matter. They MUST be exactly:
- `AICORE_ORCH_AUTH_URL` (NOT `SAP_AI_CORE_*`)
- `AICORE_ORCH_CLIENT_ID`
- etc.

These are the names `scripts/sap_coach.py` reads. If you see the coach panel say "SAP AI Core not configured", check that all 7 env vars are set in Render dashboard → service → Environment.

**Cold start is too slow**
Either upgrade to `plan: starter`, or set up a cron-job.org ping every 10 min to `/healthz`.

**iPhone can't add to home screen**
Make sure you opened the URL in **Safari**, not Chrome / in-app browser. Only Safari supports the iOS PWA install flow.