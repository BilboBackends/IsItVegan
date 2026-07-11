# VeganFind

Finds vegan-friendly dishes at restaurants by analyzing menu text and photos,
even when a restaurant doesn't market itself as vegan. See [CLAUDE.md](CLAUDE.md)
for the full product spec and pipeline design.

**Live site:** https://bilbobackends.github.io/IsItVegan/ — a fully static
build served by GitHub Pages: the consumer views plus JSON data snapshots,
no backend, no credentials, nothing a visitor can trigger. The pipeline
(scraping, classification, Admin) runs only on the local machine; see
"Publishing the public site" below.


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
  supports distance ranges / nearest-first sorting, price caps (Under
  $10/$15/$20/$30) with a cheapest sort, a meals-vs-sides filter, shareable
  dish details, correction reports, and matching map pins. Restaurant cards
  carry Google $-tier price levels and a strict vegan count: a headline
  "N vegan meals" means verdict `vegan` or high-confidence `likely_vegan`
  only — `vegan_adaptable` never counts, and sides are tallied separately so
  a bag of chips can't pose as a meal. Hearts save dishes and restaurants
  locally in the browser under the **Saved** tab—no account required. Menu
  freshness and current opening status are shown wherever that restaurant
  context is useful. On phones, filters collapse behind a compact disclosure
  and a floating List/Map pill flips views.
- **Admin** (`#admin`, local only) — the pipeline dashboard: run discovery /
  enrichment / ingestion, refresh stale menus and Google opening data, review
  correction reports, add restaurants (two-step: pick the exact Google Places
  match, then choose whether to scrape/classify immediately), and inspect
  scraped menu text and scores. Bulk scrape and classify run as background
  jobs with live progress bars that survive page reloads; classification has
  a DeepSeek classifier control, a mode
  toggle (changes-only vs full re-extraction), and a concurrency select
  (1-6 at a time — sequential when subscription quota is low). A
  Subscription-limits panel shows each provider's 5-hour/weekly usage bars.
  Each restaurant row offers view-menu, history (menu versions over time
  plus the dish-change log), rescrape, reclassify with a size-based cost
  estimate (replaced by the actual cost after a run), hide, and archive
  (archived listings move to their own tab and leave every consumer view and
  bulk run — data kept, one-click restore). Rows filter by operational
  status, group by refresh/pipeline/freshness/classification age, and sort
  by name, last-classified time, menu size, or vegan meals. An automated
  menu-quality audit flags likely-false or incomplete menus with one-click
  rescrape and human review dispositions.

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

Instead of (or on top of) area discovery, give a list of names. The CLI
resolves each via Places Text Search (biased toward the configured area; a
match is only accepted if its name actually overlaps the query) and runs the
full pipeline: enrichment, menu ingestion, and dish classification (skip
with --no-classify). Upserts on place_id, so re-adding an existing
restaurant refreshes it rather than duplicating. The dashboard's
"+ Add restaurants" is the confirm-first version: it shows up to five
candidate matches per name (weak matches flagged and unselected,
already-added places labeled), and you choose which pipeline steps run
immediately.

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
to per-section pages), always probes the conventional `/menu` path (JS-built
navs hide links from static HTML), keeps EVERY page that scores as a menu
(lunch, dinner, brunch, drinks — stored one source row per page and combined
for classification), and scores each page with a menu detector
(`menu_score.py`) — prices, food words, menu-section headers, list
structure. Homepage marketing copy is rejected rather than fed to Claude.

Every distinct menu capture is also stored as an immutable `menu_versions`
row (identical recrawls add nothing), and classification is change-aware:
menus whose text is unchanged since their last classification are SKIPPED
outright, and changed menus run in DELTA mode — the model sees the previous
dish inventory and emits only new/changed dishes plus removed names (output
tokens dominate cost, so a 3-dish change costs ~3 dishes, not ~150).
Suspicious deltas (most of the menu "removed") fall back to a full pass;
`--full` forces one. Every transition is recorded in `dish_changes`
(added / removed / price_changed / verdict_changed) — the longitudinal
record of how menus and prices drift, exposed at
`/api/restaurants/<id>/menu-versions` and `.../dish-changes`.

Every successful scrape also updates a persistent `crawl_profiles` row with
the validated menu-page URLs, successful transport (`http` or `headless`),
menu score/size, and a normalized content fingerprint. The next scheduled
recrawl tries that learned route first instead of rediscovering the menu from
the homepage. If it no longer produces a valid menu, the normal discovery
ladder runs and automatically replaces the stale profile with the new route.
Temporary failures are recorded without erasing the last known-good context.

Structured data beats DOM scraping when available (`structured_menu.py`):
every fetched page is mined for schema.org Menu JSON-LD (Popmenu et al.
embed the FULL menu for SEO) and for ordering-platform state JSON in inline
scripts — platforms that visibly render 15 items often ship 170 as data.

When plain HTTP finds nothing convincing — third-party ordering links
present, only a single section captured, or a suspiciously tiny "menu" — it
escalates to a headless browser, which scrolls in steps banking text so
lazy-loaded AND virtualized lists (items removed from the DOM as you scroll)
are captured whole, clicks through tabbed menu widgets, and navigates
same-page category fragments (Square Online) even when their nav drawer is
hidden.

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

Machine-repairable findings can also run through a bounded recursive repair
loop:

```bash
python menu_repair.py --dry-run              # show repair candidates
python menu_repair.py --max-passes 2         # audit -> scrape -> audit again
python menu_repair.py --restaurant-id 274    # repair one known restaurant
```

Each pass compares audit fingerprints and stops when the finding clears, when
evidence stops changing, or at the pass limit. Failed attempts preserve the
last validated menu. Repaired restaurant IDs are printed for change-aware
reclassification.

When keyword matching finds no menu link, a cheap LLM navigator (Claude Haiku)
picks the menu link from all the page's links — catching non-obvious labels
("Bill of Fare", "View Our Menu") the keyword list misses. Menu links that
resolve to a **PDF** are extracted too (pypdf locally; Claude's native PDF
reading as a fallback for image/scanned PDFs). When plain HTTP finds no real
menu, ingestion escalates to a headless browser (Playwright/Chromium) that runs
the page's JavaScript so JS-rendered menus (Toast/Square/Clover, SPA sites)
render before extraction. Each fallback fires only when the cheaper path fails,
so sites that scrape fine over HTTP never pay for the LLM, PDF, or browser step.

Remaining failures: bot walls that survive even a real-Chrome headless
browser, social-profile-only "websites", and menus published solely as
images. All are photo-fallback candidates (the planned Phase 4). In the
current set, **53 of 59 sites with websites yield a real menu** (~90%).

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
When a menu explicitly prints calories, the displayed value or range is stored
verbatim on the dish and shown throughout the consumer menu views; calories are
never estimated.
Older classifications default to `unclear` and remain in the meal count until
that restaurant is reclassified with the expanded schema.
Structured outputs guarantee valid JSON; truncated
or refused responses are logged as failures, never stored. False positives
(calling a dish vegan when it isn't) are treated as the worst failure mode.

```bash
python classify.py                     # classify restaurants not yet done
python classify.py --all               # re-classify everyone with a menu
python classify.py --all --full        # ...ignoring the unchanged-menu skip
python classify.py --restaurant-id 14  # one restaurant (debugging)
python classify.py --parallel 6        # up to 6 restaurants concurrently
python classify.py --mock --dry-run    # no API call, canned result
```

Bulk runs classify up to N restaurants concurrently (model calls are
I/O-bound; DB writes stay serial on the coordinating thread), and are
change-aware by default: unchanged menus are skipped, changed menus run in
delta mode. `--full` forces complete re-extraction — required after
classifier prompt/schema changes, since an unchanged menu otherwise keeps
its old verdicts.

Dishes upsert on (restaurant_id, name); each run adds a new classification row
(model version + timestamp), and reads always use the latest verdict. The
dashboard shows per-restaurant vegan-option counts and a per-dish verdict view.
Dietary fields are menu-text inferences, not allergy or cross-contact
certification. Anthropic defaults to Claude Sonnet; override it with
`ANTHROPIC_CLASSIFIER_MODEL` (the older `CLASSIFIER_MODEL` remains an alias).

Classification uses DeepSeek exclusively (`classification_providers.py`).
`CLASSIFIER_PROVIDER=deepseek` is the default; `auto` is an alias for the same
single-provider behavior. A DeepSeek failure stops that classification instead
of silently falling back to Claude, Codex, or Anthropic. Automatic classifier
guardrails, spot checks, learned-correction injection, and audit records are
disabled. Large menus are still split into bounded chunks before being sent to
DeepSeek; that is response-size handling, not a second classifier or audit.

Admin classification runs—including a single restaurant's **reclassify**
button—run in the backend and expose live provider/progress status. Reloading
the browser reconnects to the active job and does not stop it. Job state is
currently process-local: restarting or auto-reloading the Flask backend ends
the worker and cannot resume an in-flight model response.

Admin also shows each stored menu's exact character count, workload band, a
broad runtime range, and the Anthropic cost estimate when the metered API is
selected. Runtime is intentionally approximate because provider load and dish
density can matter as much as raw menu length.

## Publishing the public site

The public site is fully static: the frontend built in static-data mode plus
JSON snapshots of the consumer data, deployed to GitHub Pages by
`.github/workflows/deploy-pages.yml` on every push to master.

```bash
# After a recrawl/reclassify session — export data, commit, push, deploy:
python publish_static.py --push

# Export only (inspect frontend/public/data/*.json before committing):
python publish_static.py
```

Only consumer-facing data ships (archived/hidden/non-food venues excluded,
admin fields stripped). The global Food search snapshot is published as both
plain JSON (compatibility fallback) and deterministic gzip; static clients
stream-decompress the small gzip asset in the browser. Restaurant menus also
get individual shards, so opening one restaurant never downloads the global
index. These are static-hosting adapters behind one data-loader boundary. When
the catalog outgrows client-side search, that boundary is the migration point
for a paginated PostgreSQL search API and server-side facets. On the static
build Admin and report submission are hidden because there is no backend to
receive them.

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
config.py                   # env / settings loader (single source of config)
db.py                       # SQLite schema + read/upsert helpers + history tables
places_client.py            # Google Places API (New) client (search + candidates)
discover.py                 # discovery CLI: pull + persist area restaurants
add_restaurants.py          # add-by-name CLI (+ resolve/confirm used by Admin)
scraper.py                  # HTTP scrape + link following + structured menus + headless fallback
structured_menu.py          # schema.org JSON-LD + ordering-platform JSON menu mining
headless.py                 # Playwright fetch: scroll banking, tab clicking, fragment nav
menu_score.py               # heuristic: is this text a real menu vs homepage copy?
menu_audit.py               # automated menu-quality audit (Admin warnings)
ingest.py                   # ingestion CLI: scrape + persist menu text + versions
llm_nav.py                  # cheap LLM (Haiku) menu-link chooser + vision fallback
pdf_menu.py                 # PDF menu extraction (pypdf local + Claude PDF fallback)
enrich.py                   # Google food signals (vegetarian, editorial, rating, hours)
classifier.py               # dish extraction + vegan verdicts (full + delta modes)
classification_providers.py # DeepSeek classification transport (no fallback)
classification_exchange.py  # manual export/import classification jobs
classify.py                 # classification CLI: parallel, change-aware, history
usage_limits.py             # subscription usage windows for the limits panel
venue_filter.py             # consumer-visibility gate (types, hidden, archived)
publish_static.py           # export consumer data JSON for the public site
api.py                      # Flask JSON API for the local dashboard
tests/                      # network-free regression tests (pytest)
.github/workflows/          # GitHub Pages deploy on push
fixtures/                   # mock data for running without live APIs
frontend/                   # React + Vite + Tailwind (dashboard + public site)
```
