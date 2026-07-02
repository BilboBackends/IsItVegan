# VeganFind

Finds vegan-friendly dishes at restaurants by analyzing menu text and photos,
even when a restaurant doesn't market itself as vegan. See [CLAUDE.md](CLAUDE.md)
for the full product spec and pipeline design.

**Current phase: Phase 1 — menu-text ingestion (Maitland, FL).**
Phase 0 (discovery) is complete.

## Setup

### 1. Backend (Python)

```bash
python -m pip install -r requirements.txt
cp .env.example .env        # then edit .env and add your GOOGLE_PLACES_API_KEY
```

The `.env` file is gitignored — never commit real keys, and keep placeholders
only in `.env.example`.

### 2. Frontend (Node)

```bash
cd frontend
npm install
```

## Running the local dashboard

You need **two processes** — the Flask API and the Vite dev server.

```bash
# Terminal 1 — backend API (serves the SQLite data + discovery trigger)
python api.py                       # http://127.0.0.1:5000

# Terminal 2 — frontend dev server
cd frontend && npm run dev          # http://localhost:5173
```

Open **http://localhost:5173**. The Vite dev server proxies `/api/*` to the
Flask backend, so the browser only ever talks to our own backend — no API keys
reach the client.

From the dashboard you can browse discovered restaurants and click **Run
discovery** to (re-)pull the Maitland restaurant list from Google Places.

## Running the pipeline from the command line

Each stage is runnable in isolation (per CLAUDE.md conventions).

```bash
# Live discovery (needs GOOGLE_PLACES_API_KEY in .env):
python discover.py

# Mock discovery — no key or network needed:
python discover.py --mock fixtures/maitland_sample.json

# Preview without writing to the DB:
python discover.py --mock fixtures/maitland_sample.json --dry-run
```

### Phase 1 — menu-text ingestion

Scrapes each restaurant's website for readable menu text (stored as a
restaurant-level `text` source). Does not parse dishes — Claude does that in
Phase 3. Idempotent; re-runs upsert on (restaurant_id, url).

```bash
# Ingest restaurants that have a website but no menu text yet:
python ingest.py

# Re-scrape everything:
python ingest.py --all

# One restaurant (debug a single site):
python ingest.py --restaurant-id 30

# Preview without writing:
python ingest.py --dry-run
```

Sites that are bot-blocked (403/409), JS-rendered (no server-side text), or
non-HTML are reported as failures and skipped — they're candidates for the
photo-based fallback (a later phase). In the current Maitland set, ~32 of 51
sites scrape successfully.

## Discovery configuration (`.env`)

| Var | Default | Purpose |
|-----|---------|---------|
| `GOOGLE_PLACES_API_KEY` | — | Google Places API (New) key. Required for live runs. |
| `DISCOVERY_LAT` / `DISCOVERY_LNG` | Maitland center | Search center. |
| `DISCOVERY_RADIUS_METERS` | 4000 | Area radius to cover. |
| `DISCOVERY_CELL_RADIUS_METERS` | 1500 | Grid cell size. Smaller = more thorough, more API calls (~49 calls/run at 1500m). |
| `DISCOVERY_CITY` | Maitland | Keep only results whose address is in this city. |
| `DATABASE_PATH` | veganfind.db | SQLite file location. |

## Project layout

```
config.py              # env / settings loader (single source of config)
db.py                  # SQLite schema + read/upsert helpers
places_client.py       # Google Places API (New) client (grid search + dedup)
discover.py            # Phase 0 CLI: discover + persist restaurants
api.py                 # Flask JSON API for the local dashboard
fixtures/              # mock data for running without live APIs
frontend/              # React + Vite + Tailwind dashboard
```
