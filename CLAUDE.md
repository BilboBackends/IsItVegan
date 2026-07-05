# VeganFind — Project Instructions for Claude Code

## Project Goal

Build a web app that helps users find vegan-friendly dishes at restaurants —
even when the restaurant doesn't explicitly market itself as vegan. The app
infers vegan status per-dish by analyzing menu text and photos (from the
restaurant's website and Google Maps) rather than relying on
self-declared labels.

**MVP scope: Maitland, FL only.** Architecture should support expanding to
additional cities/areas later without a rewrite, but do not build multi-area
support until Maitland is working end-to-end.

## Product Principles

- A restaurant does not need to say "vegan" anywhere for us to correctly
  identify vegan dishes. We reason it out from ingredients, descriptions,
  and images.
- Every classification must be explainable. Never show "vegan" without
  showing the evidence (menu text excerpt or photo) that produced that
  verdict.
- Confidence matters more than binary labels. Use a graded verdict, not a
  yes/no.
- Prefer being transparently uncertain over confidently wrong. False
  positives (telling someone a dish is vegan when it isn't) are worse than
  false negatives.

## Verdict Taxonomy

Every dish gets classified into one of:

- `vegan` — high confidence, ingredients clearly plant-based
- `likely_vegan` — probable but not certain (e.g., no dairy/meat mentioned,
  but sauce/preparation unknown)
- `vegan_adaptable` — vegan if modified (e.g., "ask to hold the cheese")
- `not_vegan` — contains or likely contains animal products
- `unclear` — insufficient evidence to classify

Each verdict must store: `confidence` (0–1), `reasoning` (short text), and
`source` (which text excerpt or image supported it).

## Tech Stack

- **Backend:** Python
- **Frontend:** React + Vite, Tailwind
- **LLM:** Claude, via a provider chain (`classification_providers.py`):
  the local Claude Code CLI (subscription-billed) first, then the Codex CLI
  (ChatGPT subscription), then the metered Anthropic API — API only when
  explicitly selected, never as a silent fallback. Do NOT introduce
  LangChain/RAG scaffolding — this is a structured extraction problem, not a
  retrieval problem, and direct calls with structured JSON output are
  simpler to debug and maintain.
- **Restaurant discovery:** Google Places API
- **Database:** SQLite for MVP (single area, low volume). Design the schema
  so migrating to Postgres later is a config change, not a rewrite.
- **Scraping/fetching:** Python (httpx/requests + BeautifulSoup or similar)
  for restaurant website menus

## Architecture (pipeline order)

1. **Discovery** — pull all restaurants in Maitland via Google Places API
   (name, address, website URL, photo references, place_id)
2. **Ingestion** — scrape each restaurant's website for menu text; pull
   Google Maps dish/food photos via Places API photo references
3. **Text classification** — send scraped menu text to Claude, extract
   structured dish list + per-dish vegan verdict + reasoning
4. **Vision classification** — send dish photos to Claude's vision
   capability, produce a verdict; reconcile with text verdict (photo
   evidence can upgrade/downgrade confidence, e.g. visible cheese garnish
   despite a "vegetable curry" menu description)
5. **Storage** — persist restaurants, dishes, sources, and classifications
6. **Frontend** — restaurant list/map, dish-level filtering by verdict,
   evidence shown inline

## Data Model (starting point — refine as needed)

```
restaurants
  id, name, address, place_id, website_url, lat, lng, last_scraped_at

dishes
  id, restaurant_id, name, raw_description, price, calories, category

sources
  id, dish_id, type (text|image), content (excerpt or image_url), fetched_at

crawl_profiles
  restaurant_id, menu_urls, crawl_method, content_hash, menu_score, char_count,
  last_attempt_at, last_success_at, consecutive_failures, last_error

classifications
  id, dish_id, verdict, confidence, reasoning, source_id, model_version, created_at,
  dairy_status, gluten_status, nut_status, protein_level, serving_role,
  meal_types, key_ingredients
```

## API Key Handling

- Never expose the Anthropic API key or Google Places API key client-side.
- All LLM calls and Places API calls go through the Python backend. In local
  dev the frontend only ever talks to our own backend endpoints.
- The PUBLIC site (GitHub Pages) is fully static: built frontend + exported
  JSON snapshots (`publish_static.py`), no backend, no credentials, Admin
  unreachable. The repo is public — `.env` and `*.db` are gitignored and
  must never be committed.

## Coding Conventions

- Keep the classification pipeline stages independently runnable/testable
  (discovery, ingestion, text classification, vision classification should
  each be scriptable in isolation for debugging against a single restaurant).
- Structured LLM outputs: always request strict JSON, validate before
  storing. Reject and retry once on malformed output; log failures rather
  than silently dropping them.
- Mock-first for development: support running the pipeline against a small
  fixture set of restaurants without hitting live APIs, similar to the
  mock-first approach used in the Quickbase tooling project.
- Re-scraping: menus and photos change. Design ingestion so it can be
  re-run per-restaurant on a schedule (weekly/monthly) without duplicating
  existing dishes — upsert on (restaurant_id, dish_name). Successful crawls
  persist their validated route/method as context for the next scheduled run;
  stale learned routes must fall back to full discovery automatically.

## Explicit Non-Goals for MVP

- No user accounts / auth
- No crowd-sourced corrections yet (planned for a later phase)
- No multi-city support yet — get Maitland fully correct first
- No mobile app — responsive web only

## Current Phase

**Phases 0–3 are live** (discovery, ingestion, text classification, storage,
frontend): ~60 Maitland restaurants discovered and enriched, 53 menus scraped
(multi-page, headless-capable scraper with automated quality auditing), dish
classification with vegan verdicts, dietary attributes, and meal/side serving
roles, and the consumer Explore + Admin pipeline dashboard.

Classification runs through a provider chain (`classification_providers.py`):
Claude Code subscription first, then Codex/ChatGPT subscription, with
failover on usage limits — the metered Anthropic API is only used when
explicitly selected. Headline "vegan" counts are strict: `vegan` verdicts or
high-confidence `likely_vegan` only; `vegan_adaptable` never counts.

**Next: Phase 4 — vision classification.** Google Places photos + Claude
vision for the restaurants whose menus can't be scraped (social-only
websites, hard JS walls, photo-only menus) — the audit's "photo fallback
candidates" list is the queue.

## Resolved Early Questions (kept for context)

- Scrapability: solved for most sites via two-hop link following + headless
  browser (Toast/Square/Clover/activemenus etc.); genuinely unscrapable
  restaurants are flagged in the Admin quality audit as photo-fallback
  candidates
- Google Places quota: fine at Maitland scale (~49 calls per discovery run)
- Scraping ToS: restaurant sites are scraped politely (one-shot, low volume);
  Google Maps content is never scraped — only official Places API data
