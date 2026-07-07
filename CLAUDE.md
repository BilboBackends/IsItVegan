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
- **LLM:** a provider chain (`classification_providers.py`) with four
  transports: the local Claude Code CLI (subscription-billed), the Codex CLI
  (ChatGPT subscription), the metered Anthropic API, and the DeepSeek API
  (cheap metered tier, ~10x below Sonnet's price). `auto` = Claude then
  Codex — subscriptions only; **metered APIs (anthropic, deepseek) run only
  when explicitly selected, never as a silent fallback.** A provider that
  hits its usage limit goes into a ~20-minute cooldown while the chain fails
  over. DeepSeek output is UNTRUSTED and passes the trust loop described
  below. Do NOT introduce LangChain/RAG scaffolding — this is a structured
  extraction problem, not a retrieval problem, and direct calls with
  structured JSON output are simpler to debug and maintain.
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

## Cheap-Tier Trust Loop (DeepSeek)

Bulk classification can be handed to the cheap tier, but only inside a
measured trust loop — cheap extraction is a win only if it stays honest:

1. **Guardrails** (`guardrails.py`) — deterministic checks on every
   untrusted result before storage. Hard rule: a vegan/likely_vegan verdict
   on a dish plainly naming an animal ingredient (no mock/plant qualifier)
   is downgraded to `unclear` and flagged. Soft rules (implausible vegan
   rate, uniform confidence) flag the run without touching verdicts.
2. **Audit trail** (`classification_audits` table) — every guardrail flag,
   downgrade, and spot-check outcome is persisted; the Admin "Cheap-model
   audit" panel shows flag counts and spot-check agreement.
3. **Spot checks** (`audit_spotcheck.py`) — samples recent cheap-model
   dishes and re-verifies them with a frontier reference (Claude
   subscription by default). Adjacent verdicts (vegan/likely_vegan) count
   as agreement; real disagreements are recorded.
4. **Learning** (`learning.py` + `classifier_corrections` table) — each
   spot-check disagreement becomes a correction ("X was wrongly V1; correct:
   V2, because…"). Active corrections are injected into the CHEAP model's
   system prompt on its next run — it literally sees its recent audited
   mistakes. Frontier providers keep the unmodified baseline prompt so the
   reference stays stable. Corrections are rows, not weights: inspectable,
   and deactivated with `UPDATE classifier_corrections SET active=0`.

## Data Model

```
restaurants
  id, name, address, place_id, website_url, lat, lng, last_scraped_at,
  enrichment fields (rating, hours, price_level, …), archived,
  last_classified_hash, last_classify_cost/provider

dishes
  id, restaurant_id, name, raw_description, price, calories, category

sources
  id, dish_id, type (text|image), content (excerpt or image_url), fetched_at

crawl_profiles
  restaurant_id, menu_urls, crawl_method, content_hash, menu_score, char_count,
  last_attempt_at, last_success_at, consecutive_failures, last_error

menu_versions        -- immutable history: one row per distinct menu content
  restaurant_id, content, content_hash (UNIQUE per restaurant), menu_score,
  char_count, fetched_at

dish_changes         -- longitudinal drift: added/removed/price/verdict moves
  restaurant_id, dish_name, change_type, old/new price + verdict, observed_at

classifications
  id, dish_id, verdict, confidence, reasoning, source_id, model_version, created_at,
  dairy_status, gluten_status, nut_status, protein_level, serving_role,
  meal_types, key_ingredients

dish_votes / restaurant_votes   -- thumbs; client_id = one live vote per browser

classification_audits           -- guardrail flags + spot-check outcomes
classifier_corrections          -- learned corrections injected into cheap-model prompts
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

- No user accounts / auth (thumbs/favorites are anonymous, per-browser)
- No crowd-sourced verdict corrections yet — thumbs and dish reports are
  collected as signal, but humans don't edit verdicts through the UI
- No multi-city support yet — get Maitland fully correct first (the Prospect
  view and address search are the on-ramp, not the feature)
- No mobile app — responsive web only

## Current Phase

**Phases 0–3 are live** (discovery, ingestion, text classification, storage,
frontend): ~70 Maitland restaurants discovered and enriched, ~55 menus
scraped (multi-page, headless-capable scraper that also mines structured
data: JSON-LD, embedded ordering-platform state, and browser client-state
menus), 6,000+ classified dishes with vegan verdicts, dietary attributes,
and meal/side serving roles.

Consumer product: Restaurants + Food items + Saved tabs (list/map, compact
expandable dish rows, address-search origin picker biased to Central
Florida, favorites, thumbs with deduped vote counts, dish share/report).
Admin: pipeline dashboard with live job progress, per-restaurant costs,
subscription usage bars, menu version history, dish-change log, quality
audit, Prospect area search, and the cheap-model audit panel.

Change-aware recrawling is live: identical menu text skips classification
entirely; changed menus run in DELTA mode (classify only new/changed dishes
+ removals) with a distrust guard that falls back to full extraction.

Classification runs through the provider chain (Claude Code subscription →
Codex subscription on `auto`; Anthropic API and DeepSeek only when
explicitly selected). The DeepSeek cheap tier runs inside the trust loop
above. Headline "vegan" counts are strict: `vegan` verdicts or
high-confidence `likely_vegan` (≥ 0.75) only; `vegan_adaptable` never
counts; drinks and desserts are excluded; meals and sides are counted
separately.

The public site is GitHub Pages (fully static: built frontend + JSON
snapshots via `publish_static.py [--push]`); the live pipeline and Admin
exist only locally.

**Next: Phase 4 — vision classification.** Google Places photos + Claude
vision for the restaurants whose menus can't be scraped (social-only
websites, hard JS walls, photo-only menus) — the audit's "photo fallback
candidates" list is the queue. Also queued: multi-area expansion (area tag +
Prospect promotion, "restaurants on Mills" → Orlando).

## Resolved Early Questions (kept for context)

- Scrapability: solved for most sites via two-hop link following + headless
  browser (Toast/Square/Clover/activemenus etc.); genuinely unscrapable
  restaurants are flagged in the Admin quality audit as photo-fallback
  candidates
- Google Places quota: fine at Maitland scale (~49 calls per discovery run)
- Scraping ToS: restaurant sites are scraped politely (one-shot, low volume);
  Google Maps content is never scraped — only official Places API data
