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

Two top-level views:

- **Explore** — one consumer-facing page that opens on **Restaurants & map**
  (`#restaurants`) and switches quickly to **Food items & map** (`#dishes`).
  Food items is a searchable index across every analyzed restaurant, with
  separate food, dessert, and drink sections plus verdict and restaurant
  filters; its map pins show matching-item counts. The restaurant view keeps
  search, distance/geolocation controls, synchronized cards and map pins, and
  dish-detail modals. Google ratings and rating counts appear on restaurant
  cards, map popups, food results, and dish modals after enrichment.
  Food search also understands combined intent such as `vegan pizza`,
  `high protein breakfast`, and dietary phrases such as `dairy free`, while
  indexing normalized ingredients such as tofu, seitan, and mushroom. It
  supports distance ranges / nearest-first sorting, shareable dish details,
  correction reports, and matching map pins. Hearts save dishes and restaurants locally in the browser under the
  **Saved** tab—no account required. Menu freshness and current opening status
  are shown wherever that restaurant context is useful.
- **Admin** (`#admin`) — the pipeline dashboard: run discovery / enrichment /
  ingestion, refresh stale menus and Google opening data, review correction
  reports, add restaurants by name, and inspect scraped menu text and scores.
  Each restaurant has a persistent refresh-enabled checkbox; paused rows are
  excluded from bulk jobs but retain their one-off debug actions. Select any
  combination of enabled rows to run a background menu scrape or Claude
  reclassification with live progress and a pre-run cost estimate. Operational
  filters cover enabled/paused refreshes, missing or stale menus, classification
  stage, quality warnings, excluded venues, and missing websites; rows can be
  grouped by refresh status, pipeline stage, or menu freshness.

Consumer views automatically exclude Google place types that are not food
venues (for example convenience stores, gas stations, and supermarkets).
Excluded records remain visible and labeled in Admin, where any additional
false-positive listing can be hidden from Explore without deleting its data.

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

### Adding specific restaurants by name

Instead of (or on top of) area discovery, give a list of names. Each resolves
via Places Text Search (biased toward the configured area; a match is only
accepted if its name actually overlaps the query), then runs the full
pipeline automatically: enrichment, menu ingestion, and Claude dish
classification (~$0.10/restaurant; skip with --no-classify). Upserts on
place_id, so re-adding an existing restaurant refreshes it rather than
duplicating. Also available in the dashboard via "+ Add restaurants".

```bash
python add_restaurants.py "Ethos Vegan Kitchen" "4Rivers Smokehouse"
python add_restaurants.py --file names.txt        # one name per line
python add_restaurants.py --file names.txt --dry-run   # show matches only
python add_restaurants.py "Some Place" --no-classify   # scrape only
```

Always spot-check the printed match (name + address) — a wrong match poisons
everything downstream, so unmatchable names are reported as not-found rather
than guessed.

### Phase 1 — menu-text ingestion

Scrapes each restaurant's website for readable menu text (stored as a
restaurant-level `text` source). Does not parse dishes — Claude does that in
Phase 3. Idempotent; re-runs upsert on (restaurant_id, url).

```bash
# Ingest restaurants that have a website but no menu text yet:
python ingest.py

# Re-scrape everything:
python ingest.py --all

# Re-scrape only menus older than 30 days:
python ingest.py --stale-days 30

# One restaurant (debug a single site):
python ingest.py --restaurant-id 30

# Preview without writing:
python ingest.py --dry-run
```

The scraper follows menu-like links two hops deep (menu index pages link out
to per-section pages), keeps EVERY page that scores as a menu (lunch, dinner,
brunch, drinks — stored one source row per page and combined for
classification), and scores each page with a menu detector (`menu_score.py`)
— prices, food words, menu-section headers, list structure. Homepage
marketing copy is rejected rather than fed to Claude. When plain HTTP finds
nothing convincing — third-party ordering links present, only a single
section captured, or a suspiciously tiny "menu" — it escalates to a headless
browser, which also clicks through tabbed menu widgets that keep only the
active section in the DOM.

Quality is watched two ways so regressions surface without manual deep dives:

- `python -m pytest tests/` — fast, network-free regression tests pinning
  every scraper failure mode we've fixed (word-boundary link hints,
  multi-page keeping, social-profile guard, marketing rejection, stale-page
  pruning).
- **Menu quality warnings** in Admin (`/api/menu-quality`) — an automated
  audit of stored menus that flags likely-false or incomplete ones: tiny
  text, no prices, weak menu score, a single captured section, identical
  text shared across restaurants, or a website with nothing scraped. Each
  finding has a one-click rescrape.

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
as context) to the selected model, which extracts every dish and classifies it into the
CLAUDE.md verdict taxonomy — `vegan` / `likely_vegan` / `vegan_adaptable` /
`not_vegan` / `unclear` — with a confidence score, reasoning, and a verbatim
menu excerpt as evidence. The same pass stores ingredient-level dairy,
gluten, and nut status; protein level; meal-versus-side serving role; likely
meal contexts; and normalized key ingredients so discovery can improve without
another model call. Restaurant totals count full vegan meals separately from
sides and small plates; drinks and desserts are excluded from both totals.
Older classifications default to `unclear` and remain in the meal count until
that restaurant is reclassified with the expanded schema.
Structured outputs guarantee valid JSON; truncated
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
Dietary fields are menu-text inferences, not allergy or cross-contact
certification. Anthropic defaults to Claude Sonnet; override it with
`ANTHROPIC_CLASSIFIER_MODEL` (the older `CLASSIFIER_MODEL` remains an alias).

Classification transport is provider-independent. `CLASSIFIER_PROVIDER=auto`
prefers the locally installed Codex CLI when it is logged in with ChatGPT, then
uses Anthropic when Codex is unavailable before a request starts. Set it to
`codex` or `anthropic` to force one provider; the Admin provider selector can
also choose per run. Codex runs are ephemeral and read-only and use JSON-schema
structured output. A failed/limited Codex request never silently falls through
to a billable Anthropic retry.

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
