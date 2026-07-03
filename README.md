# VeganFind

Finds vegan-friendly dishes at restaurants by analyzing menu text and photos,
even when a restaurant doesn't market itself as vegan. See [CLAUDE.md](CLAUDE.md)
for the full product spec and pipeline design.

**Current phase: Phase 3 — dish classification (Maitland, FL).**
Phase 0 (discovery) and Phase 1 (menu-text ingestion) are complete.

## Setup

### 1. Backend (Python)

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium   # headless browser for JS-rendered menus
cp .env.example .env        # then edit .env and add your GOOGLE_PLACES_API_KEY
```

The Chromium download is a one-time step Playwright needs on top of the pip
install. Ingestion still runs without it (HTTP-only), just with lower menu
coverage.

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

The scraper follows menu-like links one level deep (same domain) so it finds
menus that aren't on the landing page, and scores each page with a menu
detector (`menu_score.py`) — prices, food words, menu-section headers, list
structure. Only pages that clear the menu threshold are stored, so homepage
marketing copy ("Authentic Cuisine · Reserve Your Table") is rejected rather
than fed to Claude.

When keyword matching finds no menu link, a cheap LLM navigator (Claude Haiku)
picks the menu link from all the page's links — catching non-obvious labels
("Bill of Fare", "View Our Menu") the keyword list misses. Menu links that
resolve to a **PDF** are extracted too (pypdf locally; Claude's native PDF
reading as a fallback for image/scanned PDFs). When plain HTTP finds no real
menu, ingestion escalates to a headless browser (Playwright/Chromium) that runs
the page's JavaScript so JS-rendered menus (Toast/Square/Clover, SPA sites)
render before extraction. Each fallback fires only when the cheaper path fails,
so sites that scrape fine over HTTP never pay for the LLM, PDF, or browser step.

Remaining failures: bot-blocked even in a browser, menus reachable only via a
JS "Order" button we don't click, non-HTML (PDF/image), or genuinely
homepage-only. All are photo-fallback candidates. In the current Maitland set,
**35 of 51 sites yield a real menu** (~74% of the non-gas-station spots).

### Phase 3 — dish classification

Sends each scraped menu (plus Google's editorial summary and vegetarian flag
as context) to Claude, which extracts every dish and classifies it into the
CLAUDE.md verdict taxonomy — `vegan` / `likely_vegan` / `vegan_adaptable` /
`not_vegan` / `unclear` — with a confidence score, reasoning, and a verbatim
menu excerpt as evidence. Structured outputs guarantee valid JSON; truncated
or refused responses are logged as failures, never stored. False positives
(calling a dish vegan when it isn't) are treated as the worst failure mode.

```bash
python classify.py                     # classify restaurants not yet done
python classify.py --all               # re-classify everyone with a menu
python classify.py --restaurant-id 14  # one restaurant (debugging)
python classify.py --mock --dry-run    # no API call, canned result
```

Dishes upsert on (restaurant_id, name); each run adds a new classification row
(model version + timestamp), and reads always use the latest verdict. The
dashboard shows per-restaurant vegan-option counts and a per-dish verdict view.
Model defaults to Claude Opus (accuracy-critical); override with
`CLASSIFIER_MODEL`.

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
scraper.py             # Phase 1: HTTP scrape + menu-link following + headless fallback
headless.py            # Playwright headless-browser fetch (JS-rendered menus)
menu_score.py          # Heuristic: is this text a real menu vs homepage copy?
ingest.py              # Phase 1 CLI: scrape + persist menu text
llm_nav.py             # Cheap LLM (Haiku) menu-link chooser + vision fallback
pdf_menu.py            # PDF menu extraction (pypdf local + Claude PDF fallback)
enrich.py              # Pull Google food signals (vegetarian, editorial, type)
classifier.py          # Phase 3: Claude dish extraction + vegan verdicts
classify.py            # Phase 3 CLI: classify + persist dishes/verdicts
api.py                 # Flask JSON API for the local dashboard
fixtures/              # mock data for running without live APIs
frontend/              # React + Vite + Tailwind dashboard
```
