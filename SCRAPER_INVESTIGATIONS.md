# Scraper investigations

This is the durable handoff log for site-specific crawl failures that expose
general scraper weaknesses. Each entry records observed evidence, root cause,
the reusable fix, and acceptance criteria. Do not treat a site-specific URL or
API route as the fix unless the entry explicitly says it is stable.

## 2026-07-05 — Olive Garden, Winter Park Village (restaurant 87)

### Summary

The crawl was recorded as successful, but it captured only Olive Garden's
`/specials` marketing page. The page belonged to the site's default Times
Square context rather than the requested Winter Park restaurant. It contained
enough prices and food words to clear the generic menu score, became the saved
learned route, and left the restaurant with zero classified dishes.

This is not primarily a bot-block or missing-data problem. Olive Garden's full
location menu is available to the browser. The crawler loses the location
session between pages and does not extract the structured menu JSON that the
site places in browser storage.

No production data was changed during this investigation. Reproductions were
read-only and were not persisted.

### Stored state at time of investigation

| Field | Observed value |
| --- | --- |
| Restaurant | Olive Garden Italian Restaurant, 665 N Orlando Ave, Winter Park |
| Website location id | `1275` |
| Saved menu URL | `https://www.olivegarden.com/specials` |
| Saved method | `headless` |
| Saved menu score | `0.706` |
| Saved characters | `2,792` |
| Saved pages | `1` |
| Dishes produced | `0` |
| Wrong location visible in saved text | NYC / Times Square |

The existing quality audit correctly flags the stored result after ingestion:

- only one menu section captured (`/specials`)
- partially captured ordering page (few priced items alongside order/cart chrome)

The problem is that these checks run after the scraper has already called the
result a success and saved `/specials` as the next learned route.

### Reproduction results

Plain HTTP returned JavaScript shells with zero extracted text for both the
location URL and `/menu`.

The existing headless discovery opened every followed URL in a new browser
session and produced these relevant candidates:

| Candidate | Characters | Score | Result |
| --- | ---: | ---: | --- |
| Winter Park location page | 2,011 | 0.197 | Correct location, not a menu |
| `/menu/classic-entrees` | 354 | 0.200 | Location missing; “find a restaurant” shell |
| `/order-online` | 1,991 | 0.406 | Ordering chrome, not enough menu content |
| `/specials` | 2,792 | 0.706 | False positive; promotional offers only |
| `/menu` | 354 | 0.200 | Location missing; “find a restaurant” shell |
| `/menu/family-style-meals` | 354 | 0.200 | Location missing; “find a restaurant” shell |

Opening the location page and then `/menu/classic-entrees` in the **same**
browser context changed the result:

| Signal | Persistent-session result |
| --- | ---: |
| Correct restaurant retained | Winter Park Village / restaurant `1275` |
| Visible menu characters | 3,506 before exhaustive category extraction |
| Menu score | 0.984 |
| Visible prices | 16 |
| Browser-storage menu JSON | 276,468 characters |
| Top-level categories | 16 |
| Product references | 220 |
| Unique products | 197 |

The structured payload contains names, descriptions, formatted prices,
calories, category/subcategory hierarchy, product ids, and configuration
metadata. It covers family meals, appetizers, limited-time items, classic
entrees, soups/salad/breadsticks, lighter portions, create-your-own pasta,
lunch, desserts, sides, beverages, wine/beer, gluten-sensitive items, kids,
and catering.

The location page creates restaurant-selection state, including the
`DRIRESTAURANTID` and `DRIREST` cookies. The menu page then requests its menu
for `restaurantNum=1275` and caches the response in local storage. Endpoint
names are obfuscated/versionable and must not be hardcoded as the solution.

### Root-cause chain

1. Olive Garden requires restaurant selection before it exposes menu data.
2. `headless.fetch_rendered_html()` launches and closes a browser for every
   URL.
3. `scraper._collect_headless()` renders the location landing page, extracts
   links, and then renders each link through separate calls to
   `fetch_rendered_html()`.
4. The restaurant cookies/local storage established on the landing page are
   discarded before menu category pages are opened.
5. Those pages render location-picker shells instead of menu products.
6. `/specials` renders independently and has seven price-like strings, nine
   food-word hits, two section-word hits, and many short lines. The current
   page-level heuristic therefore scores it as a menu.
7. The headless result path does not apply the single-section completeness
   rejection that the HTTP path applies in `scrape_menu_text()`.
8. Ingestion saves `/specials` as a successful learned route.
9. Future recrawls try that learned route first. Its character count remains
   similar, so the size-regression fallback does not trigger rediscovery.
10. The complete menu JSON is present in browser storage, but
    `structured_menu.py` only examines HTML script tags. Its generic aliases
    also do not cover Olive Garden's `displayName`, `formattedPrice`, and
    nested `nutrition.cal` shape.

### Implementation status — completed 2026-07-05

- Headless discovery and learned recrawls now reuse one isolated browser
  context per restaurant.
- Rendered local/session-storage JSON is mined generically for nested category
  and product records, including prices and calories.
- A validated structured payload stops further expensive candidate renders.
- Completeness validation now applies to HTTP, headless, mock, and learned
  results; weak single-section routes are rejected before persistence.
- The old weak `/specials` profile is automatically invalidated.
- Candidate score/keep/reject diagnostics are attached to scrape results and
  Admin progress events without cookie or storage secrets.
- Regression coverage increased from 57 to 64 backend tests.
- Restaurant 87 was recrawled successfully: 197 structured products, 35,105
  characters, score 0.911, correct Winter Park source, and no quality-audit
  finding. A subsequent learned-route dry-run returned the same hash/content.

Persistent historical candidate logs and explicit cross-checking of a
rendered location label against Google address metadata remain worthwhile
observability enhancements, but they are not required for the corrected Olive
Garden crawl path.

### Required scraper updates

#### P0 — preserve browser state while following a site

Refactor the headless collector so a crawl owns one browser + context for the
landing page and every same-site candidate. Navigating from the location page
to menu pages must retain cookies, local storage, session storage, and other
site state.

Preferred shape:

- add a reusable browser-session/context abstraction in `headless.py`
- let `_collect_headless()` open the landing page, discover links, and visit
  them in that same context
- keep a fresh isolated context between different restaurants
- retain the existing single-URL helper for callers that do not need a crawl
  session

Do not copy only the two Olive Garden cookies. Session preservation is the
generic behavior needed by any location-sensitive chain site.

#### P0 — reject partial headless successes before persistence

Move completeness validation into a shared final decision applied equally to
HTTP, headless, and learned-route results.

At minimum:

- a lone URL whose path names a section (`specials`, `breakfast`, `desserts`,
  etc.) cannot be learned as a complete menu
- order/cart chrome plus a small number of priced products is partial, even if
  the generic score clears `MENU_THRESHOLD`
- a location-specific restaurant crawl must reject content that visibly names
  a different selected location
- severe `menu_audit` rules should run before `record_crawl_success()`, not only
  after the bad source has replaced existing data
- a partial result may be retained as diagnostic evidence, but it must not
  replace the last validated menu route/content

The current HTTP-only guard at `scraper.py` around the single-section check is
not sufficient; the false success came from the headless return path.

#### P0 — invalidate sticky bad learned routes

Learned routes must pass the same completeness checks as newly discovered
routes. Size comparison alone cannot detect a consistently wrong page.

Add one of:

- a crawl-profile validation/schema version that forces rediscovery when the
  validation rules change, or
- revalidate every learned result and discard routes that now produce severe
  quality flags

After the fixes land, invalidate restaurant 87's saved `/specials` route and
recrawl it. Do not recrawl before the persistent-session/structured extraction
fix exists, or the same route will likely be learned again.

#### P1 — mine structured state produced by rendered applications

After a rendered page settles, inspect bounded JSON values from local storage,
session storage, and same-origin JSON responses. Feed promising objects into a
generic recursive product extractor.

The extractor should recognize configurable aliases and nesting, including:

- name: `displayName`, existing item-name aliases
- description: `description`, `longDescription`
- price: `formattedPrice`, nested `price.value`, existing aliases
- calories: `nutrition.cal`, `nutrition.fdamessage`
- hierarchy: `categories`, `subCategories`, `products`
- stable dedupe: product id first, then normalized name + price

Only accept a payload after structural validation (for example, at least eight
product-shaped records across plausible categories). Bound inspected value
size/count and strip unrelated configuration objects so a large SPA cache does
not inflate classification cost.

Prefer generic shape detection over hardcoding Olive Garden's current
obfuscated menu endpoint. The endpoint can change while the product/category
shape and browser state behavior remain discoverable.

#### P1 — separate “menu-likeness” from “menu completeness”

`menu_score.py` answers whether text resembles menu content. It cannot by
itself establish that the menu is complete. Add a separate completeness result
with evidence such as:

- structured product count
- priced item-block count (name/description/price adjacency, not independent
  page-wide word counts)
- number of distinct sections
- single-section URL detection
- partial ordering-page markers
- selected-location match/unknown/mismatch
- size and product-count comparison with the last validated version

Store both values. A specials page can legitimately be menu-like while still
being incomplete.

#### P2 — improve crawl-attempt observability

The current profile stores only the last success/failure summary. Add a
bounded `crawl_attempts`/`crawl_candidate_logs` history (or structured JSONL
equivalent) so the Admin view can explain a crawl without reproducing it.

For each candidate log:

- crawl/run id, restaurant id, timestamp, elapsed milliseconds
- stage (`learned`, `http`, `headless`, `structured-storage`, `pdf`)
- requested and final URL
- HTTP/browser outcome and concise error
- character count and score breakdown (prices, food words, sections, lines)
- structured product/category counts
- selected-location identity or mismatch (never store session secrets)
- kept/rejected status and exact decision reason
- whether state came from a persistent browser context

For the overall decision log:

- why discovery escalated or stopped
- which candidate won and why
- completeness flags
- whether existing content/profile was preserved
- whether learned context was accepted or invalidated

Never log cookie values, authorization headers, full browser storage, or other
session secrets. Cookie names and redacted counts are enough for diagnostics.

### Regression fixtures and acceptance criteria

Add an offline fixture shaped like the observed client-state JSON; do not make
CI depend on Olive Garden's live website.

Required tests:

1. A location landing page followed by a menu page uses the same browser
   context.
2. The fixture extracts all category levels, product names, descriptions,
   prices, and calories.
3. Duplicate products referenced by multiple categories dedupe by product id.
4. A `/specials`-only result is marked partial and cannot become a successful
   learned route.
5. The same completeness validation runs for HTTP, headless, and learned
   results.
6. A selected-location mismatch rejects or quarantines the candidate.
7. A severe partial result never replaces a previously validated full menu.
8. Candidate logs explain every keep/reject decision without recording cookie
   values or raw session storage.
9. The Olive Garden fixture yields approximately the observed 16 top-level
   categories and 197 unique products (fixture count should be exact once a
   sanitized snapshot is created).

### Useful official references

- Current category route: <https://www.olivegarden.com/menu/classic-entrees>
- Official menu site map: <https://www.olivegarden.com/site-map>
- Official vegetarian/vegan information:
  <https://www.olivegarden.com/nutrition/vegetarian-and-vegan-options>

The vegetarian/vegan guide is excellent classification evidence but is not a
replacement for the full location menu: it intentionally lists only dietary
options, not every product available at restaurant 1275.
