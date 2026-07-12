---
name: scrape-doctor
description: Deep-dive a restaurant whose menu scrape failed or came back incomplete, find the root cause, fix the scraper GENERICALLY, verify, and commit. Invoke with a restaurant id or name.
---

# Scrape Doctor

You are debugging why the VeganFind scraper failed (or silently captured a
partial/wrong menu) for ONE restaurant, then fixing the scraper so the whole
CLASS of failure can't happen again. This skill encodes hard-won lessons —
follow the order; the shortcuts are traps we already fell into.

## Ground rules

- **Fix the class, never the site.** Every scraper improvement in this repo
  came from one restaurant but shipped as a generic mechanism (word-boundary
  hints, daypart probing, PDF freshness, location filtering). A hack keyed to
  one domain is a failure — if no generic fix exists, the honest verdict is
  `unscrapeable`.
- **Reproduce with the scraper's own code and headers** (`scraper._HTTP_HEADERS`),
  never a browser UA. Sites serve different redirects/content to the bot UA
  (Sixty Vines 302'd browsers to /brunch but served brunch AT the base URL to
  the bot). A diagnosis made with the wrong UA produces a fix that won't hold.
- **Zero LLM calls inside the scraper.** Fixes are deterministic Python.
- Never touch `.env`, never push, never edit the DB other than reads
  (verification re-scrapes go through `ingest.run`, which persists normally).
- Read `MEMORY.md` / project memories if available — several scraper gotchas
  are recorded there.

## Step 1 — Load the stored evidence (no network yet)

From `veganfind.db` (sqlite3, read-only mindset):
- `restaurants` row: id, name, website_url, address.
- `crawl_profiles`: menu_urls, crawl_method, menu_score, char_count,
  consecutive_failures, last_error, last_diagnostics.
- `sources` (type='text', excluding `google:editorial_summary`): what was
  last stored, per-URL sizes.
- `menu_versions`: char_count history — a sudden shrink or growth is a clue.

## Step 2 — Reproduce

```python
from scraper import scrape_menu_text
result = scrape_menu_text(website_url, use_headless=False,
                          crawl_context=<profile or None>, address=<address>)
```
Record: ok, error, menu_score, char_count, pages, diagnostics. Run once with
the learned context and once with `crawl_context=None` (full discovery) —
they can differ. If headless is relevant, try `use_headless=True` too.

## Step 3 — Investigate the live site (scraper's headers!)

Check, in rough likelihood order (these are the classes we've actually hit):

1. **Menu links invisible to href-parsing** — JS-built navs, client-side
   daypart tabs, serialized builder JSON. Compare raw-HTML `<a href>`s
   against what a human sees.
2. **Silent redirects** — request URL vs `resp.url` final URL; and whether
   the final content differs by UA (fetch once with `_HTTP_HEADERS`, once
   with a plain browser UA; if they differ, say so explicitly).
3. **Single-section/daypart capture** — the stored menu is one meal period;
   probe sibling/child daypart paths (the scraper has `_probe_daypart_pages`;
   maybe the site's shape needs a new probe direction).
4. **PDF issues** — menu is a PDF: check it downloads, has a text layer, and
   is FRESH (`pdf_menu.is_pdf_stale`; Pepe's served a Cinco de Mayo 2023 PDF
   for years). An image-only PDF without dates is a Claude-extraction case.
5. **Wrong location** — multi-location brand serving another city's menu
   page (location_filter should anchor; check `filter_location_urls`).
6. **Ordering-platform walls** — Toast/Clover/etc. 403 plain HTTP; check
   whether headless handles it or the platform needs a collector like the
   Viguest one.
7. **Structured data present but unparsed** — JSON-LD / embedded state in
   the page that `structured_menu.py` doesn't recognize yet.
8. **Genuinely unscrapeable** — social-only site, photo-only menu, hard bot
   wall even for headless. STOP here: verdict `unscrapeable`, recommend the
   photo-fallback queue. Do not hack.

## Step 4 — Fix generically

Change `scraper.py` / `pdf_menu.py` / `structured_menu.py` /
`location_filter.py` following their existing idioms (module constants,
guard functions, bounded probes, diagnostics entries). Keep the change as
small as the class allows. Write the code comment for the constraint, not
the story.

## Step 5 — Regression test

Add a network-free test to the matching `tests/test_*.py`, following that
file's style (each test pins a real failure mode with a comment naming the
restaurant that exposed it).

## Step 6 — Verify (all three, in order)

1. `python -m pytest tests/ -q` — everything passes.
2. Live re-scrape of the target: `scrape_menu_text(...)` returns `ok=True`,
   `is_menu=True`, plausible `char_count`, and content spot-checks contain
   dishes you saw on the live site.
3. No collateral damage: live learned-route scrape of 2 known-good
   restaurants (e.g. ids 124, 110) still succeeds with roughly unchanged
   char counts.

If verification fails and you cannot fix it, `git checkout` your changes and
report honestly.

## Step 7 — Commit handoff and report

When running through Claude, commit (never push) with a message following repo
convention: what class of failure, which restaurant exposed it, how it is now
guarded, and verification results.

When the kickoff prompt identifies the Codex CLI, `.git` is intentionally
read-only. Do not attempt `git add` or `git commit`. Leave only the intended
Python source and regression-test changes in the worktree. The trusted launcher
validates the changed paths and creates the commit after a `fixed` result.

End your FINAL message with exactly one line so the caller can parse it:

```
SCRAPE-DOCTOR RESULT: <fixed|unscrapeable|failed> — <one-sentence summary>
```

- `fixed` — scraper changed and verified; committed directly by Claude or by
  the trusted Codex launcher handoff.
- `unscrapeable` — no generic fix exists; name the reason (photo-only,
  social-only, bot wall) so the restaurant goes to the photo-fallback queue.
- `failed` — you could not complete the diagnosis/fix; say what you learned
  and what a human should look at.
