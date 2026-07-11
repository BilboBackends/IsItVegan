"""SQLite persistence layer.

Schema mirrors the data model in CLAUDE.md. SQLite is the MVP store; the
schema is kept plain (no SQLite-only features) so migrating to Postgres later
is a config change, not a rewrite.

Beyond the core tables (restaurants/dishes/sources/classifications), the
schema carries pipeline memory: crawl_profiles (learned scrape routes),
menu_versions (every distinct menu capture), dish_changes (menu drift over
time), and menu_quality_reviews (human audit dispositions).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from config import settings
from dish_identity import dish_identity_key, preferred_dish_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    address         TEXT,
    place_id        TEXT NOT NULL UNIQUE,
    website_url     TEXT,
    lat             REAL,
    lng             REAL,
    last_scraped_at TEXT,
    consumer_hidden INTEGER NOT NULL DEFAULT 0,
    refresh_enabled INTEGER NOT NULL DEFAULT 1,
    -- Structured food signals from Google Places (New) Place Details.
    -- Nullable: Google doesn't populate these for every restaurant.
    serves_vegetarian INTEGER,   -- 1 / 0 / NULL (unknown)
    price_level       TEXT,      -- e.g. PRICE_LEVEL_MODERATE
    primary_type      TEXT,      -- e.g. thai_restaurant
    editorial_summary TEXT,      -- Google's short blurb (often names dishes)
    rating            REAL,      -- Google user rating, 1.0â€“5.0
    user_rating_count INTEGER,   -- number of Google ratings behind `rating`
    open_now          INTEGER,   -- Google currentOpeningHours.openNow
    opening_hours     TEXT,      -- JSON weekday descriptions
    hours_enriched_at TEXT,      -- tracks one-time opening-hours backfill
    enriched_at       TEXT       -- when food signals were last fetched
);

CREATE TABLE IF NOT EXISTS dishes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    name            TEXT NOT NULL,
    raw_description TEXT,
    price           TEXT,
    calories        TEXT,   -- explicit menu value/range; never estimated
    category        TEXT,   -- food | drink | dessert (NULL = assume food)
    UNIQUE (restaurant_id, name)
);

-- A source is evidence: a menu-text excerpt or an image. It can attach to a
-- whole restaurant (raw menu text scraped in Phase 1, before dishes exist) or
-- to a specific dish (once classification produces dishes). Exactly one of
-- restaurant_id / dish_id is set. `url` records where the text/image came from.
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER REFERENCES restaurants(id),
    dish_id       INTEGER REFERENCES dishes(id),
    type          TEXT NOT NULL CHECK (type IN ('text', 'image')),
    content       TEXT NOT NULL,
    url           TEXT,
    fetched_at    TEXT,
    CHECK (
        (restaurant_id IS NOT NULL AND dish_id IS NULL)
        OR (restaurant_id IS NULL AND dish_id IS NOT NULL)
    )
);

-- One menu-text source per restaurant+url, so re-scraping upserts in place.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_restaurant_url
    ON sources (restaurant_id, url)
    WHERE restaurant_id IS NOT NULL;

-- What worked the last time this restaurant was crawled. Recrawls try these
-- validated menu pages/method first, then fall back to full discovery if the
-- site changed. `menu_urls` is a JSON array because menus often span sections.
CREATE TABLE IF NOT EXISTS crawl_profiles (
    restaurant_id       INTEGER PRIMARY KEY REFERENCES restaurants(id)
                        ON DELETE CASCADE,
    menu_urls            TEXT NOT NULL DEFAULT '[]',
    crawl_method         TEXT,
    content_hash         TEXT,
    menu_score           REAL,
    char_count           INTEGER,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

-- Every DISTINCT menu capture, kept forever: raw material for menu history,
-- price-fluctuation analysis, and delta classification. Recrawls that find
-- identical content don't add rows (UNIQUE on the fingerprint).
CREATE TABLE IF NOT EXISTS menu_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    content_hash  TEXT NOT NULL,
    content       TEXT NOT NULL,
    menu_score    REAL,
    char_count    INTEGER,
    fetched_at    TEXT NOT NULL,
    UNIQUE (restaurant_id, content_hash)
);

-- Dish-level differences between successive classified menus: what appeared,
-- what vanished, what changed price or verdict. Not user-facing (yet) — this
-- is the longitudinal record of how menus drift.
CREATE TABLE IF NOT EXISTS dish_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    observed_at   TEXT NOT NULL,
    change_type   TEXT NOT NULL CHECK (change_type IN
                      ('added', 'removed', 'price_changed', 'verdict_changed')),
    dish_name     TEXT NOT NULL,
    old_price     TEXT,
    new_price     TEXT,
    old_verdict   TEXT,
    new_verdict   TEXT
);

CREATE TABLE IF NOT EXISTS classifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id       INTEGER NOT NULL REFERENCES dishes(id),
    verdict       TEXT NOT NULL CHECK (verdict IN (
                      'vegan', 'likely_vegan', 'vegan_adaptable',
                      'not_vegan', 'unclear')),
    confidence    REAL NOT NULL,
    reasoning     TEXT,
    source_id     INTEGER REFERENCES sources(id),
    model_version TEXT,
    created_at    TEXT,
    dairy_status  TEXT NOT NULL DEFAULT 'unclear',
    gluten_status TEXT NOT NULL DEFAULT 'unclear',
    nut_status    TEXT NOT NULL DEFAULT 'unclear',
    protein_level TEXT NOT NULL DEFAULT 'unclear',
    serving_role  TEXT NOT NULL DEFAULT 'unclear',  -- meal | side | unclear
    meal_types    TEXT NOT NULL DEFAULT '[]',
    key_ingredients TEXT NOT NULL DEFAULT '[]'
);

-- Lightweight thumbs from dish rows (agree/disagree with a verdict or
-- just liked/disliked the dish). Votes also live in the visitor's
-- localStorage; this table only records what the LOCAL app sees.
CREATE TABLE IF NOT EXISTS dish_votes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id       INTEGER REFERENCES dishes(id) ON DELETE SET NULL,
    dish_name     TEXT,
    restaurant_id INTEGER,
    vote          TEXT NOT NULL CHECK (vote IN ('up', 'down')),
    -- Anonymous per-browser id so one visitor holds ONE live vote per dish
    -- (re-clicks update or clear it) instead of stacking rows. NULL on rows
    -- recorded before client ids existed.
    client_id     TEXT,
    created_at    TEXT NOT NULL
);

-- Monitoring trail for cheap-model classification: every guardrail flag or
-- downgrade (guardrails.py) and every spot-check comparison against a
-- frontier model (audit_spotcheck.py) lands here, so trust in the cheap
-- tier is measured, not assumed.
CREATE TABLE IF NOT EXISTS classification_audits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id    INTEGER,
    dish_name        TEXT,
    provider         TEXT NOT NULL,
    model            TEXT,
    check_type       TEXT NOT NULL,       -- guardrail | spot_check
    rule             TEXT,                -- which check fired / 'verdict_match'
    status           TEXT NOT NULL,       -- flagged | downgraded | agree | disagree
    detail           TEXT,
    expected_verdict TEXT,                -- reference model's verdict (spot checks)
    actual_verdict   TEXT,                -- cheap model's verdict
    created_at       TEXT NOT NULL
);

-- The cheap model's "memory": corrections distilled from spot-check
-- disagreements (or added manually). Active corrections are injected into
-- its prompt as learned examples — learning.py builds the block.
CREATE TABLE IF NOT EXISTS classifier_corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_name       TEXT NOT NULL,
    description     TEXT,
    wrong_verdict   TEXT NOT NULL,
    correct_verdict TEXT NOT NULL,
    note            TEXT,                 -- why the correction is right
    source          TEXT NOT NULL DEFAULT 'spot_check',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- Same lightweight thumbs, aimed at a whole restaurant. client_id keeps one
-- live vote per browser per restaurant, exactly like dish_votes.
CREATE TABLE IF NOT EXISTS restaurant_votes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
    vote          TEXT NOT NULL CHECK (vote IN ('up', 'down')),
    client_id     TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
    dish_id       INTEGER REFERENCES dishes(id),
    dish_name     TEXT,
    issue_type    TEXT NOT NULL CHECK (issue_type IN (
                      'animal_ingredient', 'dish_removed',
                      'wrong_restaurant', 'other')),
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'resolved')),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);

-- Human disposition of an automated menu-audit finding. The fingerprint
-- binds the decision to the exact menu/flags reviewed; changed source text or
-- changed audit findings automatically become active again.
CREATE TABLE IF NOT EXISTS menu_quality_reviews (
    restaurant_id INTEGER PRIMARY KEY REFERENCES restaurants(id)
                  ON DELETE CASCADE,
    fingerprint   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('verified', 'known_issue')),
    note          TEXT,
    reviewed_at   TEXT NOT NULL
);

-- Dismissed Tier-0 dish audit findings (dish_audit.py). Keyed by the dish
-- and the finding code, with a fingerprint of the flagged value: dismissing
-- "this 1980-cal side is fine" sticks, but if the value later changes the
-- fingerprint differs and the finding resurfaces for a fresh look.
CREATE TABLE IF NOT EXISTS dish_audit_reviews (
    dish_id      INTEGER NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    code         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    note         TEXT,
    reviewed_at  TEXT NOT NULL,
    PRIMARY KEY (dish_id, code)
);

-- The read models (dish/restaurant lists) run correlated subqueries per row
-- (latest classification, vote counts, menu freshness). Without these
-- indexes each one is a full table scan and the API visibly drags once the
-- data outgrows a single town.
CREATE INDEX IF NOT EXISTS idx_classifications_dish
    ON classifications(dish_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_dishes_restaurant ON dishes(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_sources_restaurant ON sources(restaurant_id, type);
CREATE INDEX IF NOT EXISTS idx_dish_votes_dish ON dish_votes(dish_id, vote);
CREATE INDEX IF NOT EXISTS idx_restaurant_votes_restaurant
    ON restaurant_votes(restaurant_id, vote);
CREATE INDEX IF NOT EXISTS idx_menu_versions_restaurant
    ON menu_versions(restaurant_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_dish_changes_restaurant
    ON dish_changes(restaurant_id, observed_at DESC);
"""


@contextmanager
def connect(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection with foreign keys on and row access by column name."""
    path = db_path or settings.database_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the initial schema shipped. CREATE TABLE IF NOT EXISTS
# won't add them to an existing DB, so we ALTER any that are missing.
_MIGRATIONS = {
    "restaurants": {
        "consumer_hidden": "INTEGER NOT NULL DEFAULT 0",
        "refresh_enabled": "INTEGER NOT NULL DEFAULT 1",
        "serves_vegetarian": "INTEGER",
        "price_level": "TEXT",
        "primary_type": "TEXT",
        "editorial_summary": "TEXT",
        "rating": "REAL",
        "user_rating_count": "INTEGER",
        "open_now": "INTEGER",
        "opening_hours": "TEXT",
        "hours_enriched_at": "TEXT",
        "enriched_at": "TEXT",
        # Actual $ cost of the last classification run (estimated from token
        # usage) — shown next to the per-row reclassify button in Admin.
        "last_classify_cost": "REAL",
        "last_classify_provider": "TEXT",
        # Hash of the menu text the last classification ran on — when a
        # recrawl produces identical text, reclassification is skipped.
        "last_classified_hash": "TEXT",
        # Archived listings leave the Admin working set AND all consumer
        # views/pipeline runs (7-Eleven and friends). Data is kept.
        "archived": "INTEGER NOT NULL DEFAULT 0",
        # Google businessStatus: OPERATIONAL | CLOSED_TEMPORARILY |
        # CLOSED_PERMANENTLY. Enrichment auto-archives permanent closures.
        "business_status": "TEXT",
    },
    "dishes": {
        # food | drink | dessert — drinks are excluded from the headline
        # "vegan options" count (a list of vegan sodas isn't the product).
        "category": "TEXT",
        # Verbatim calorie value/range when printed on the menu.
        "calories": "TEXT",
    },
    "classifications": {
        # Ingredient-level discovery attributes inferred alongside the vegan
        # verdict. JSON arrays keep the SQLite MVP flexible for multi-value
        # meal and ingredient tags.
        "dairy_status": "TEXT NOT NULL DEFAULT 'unclear'",
        "gluten_status": "TEXT NOT NULL DEFAULT 'unclear'",
        "nut_status": "TEXT NOT NULL DEFAULT 'unclear'",
        "protein_level": "TEXT NOT NULL DEFAULT 'unclear'",
        # meal | side | unclear — keeps "vegan options" from counting a bag
        # of chips the same as a sandwich.
        "serving_role": "TEXT NOT NULL DEFAULT 'unclear'",
        "meal_types": "TEXT NOT NULL DEFAULT '[]'",
        "key_ingredients": "TEXT NOT NULL DEFAULT '[]'",
        # alcoholic | non_alcoholic | unclear — a Coke and a Negroni are not
        # the same kind of "drink"; the Drinks tab sections on this.
        "alcohol_status": "TEXT NOT NULL DEFAULT 'unclear'",
    },
    "reports": {
        "dish_name": "TEXT",
    },
    "dish_votes": {
        "client_id": "TEXT",
    },
    "crawl_profiles": {
        # JSON list of per-URL scrape diagnostics from the last FAILED
        # attempt (stage, score, decision, prices, food words) — the "why
        # did this menu fail" evidence for the Admin scrape-failures panel.
        "last_diagnostics": "TEXT",
    },
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, coltype in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist and add any new columns. Idempotent."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def upsert_restaurants(
    restaurants: Iterable[dict], db_path: str | None = None
) -> tuple[int, int]:
    """Insert or update restaurants keyed on place_id (idempotent re-runs).

    Each dict should carry: name, address, place_id, website_url, lat, lng,
    last_scraped_at. Returns (inserted_or_updated_count, total_seen).
    """
    rows = list(restaurants)
    with connect(db_path) as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO restaurants
                    (name, address, place_id, website_url, lat, lng,
                     primary_type, last_scraped_at)
                VALUES
                    (:name, :address, :place_id, :website_url, :lat, :lng,
                     :primary_type, :last_scraped_at)
                ON CONFLICT(place_id) DO UPDATE SET
                    name            = excluded.name,
                    address         = excluded.address,
                    website_url     = excluded.website_url,
                    lat             = excluded.lat,
                    lng             = excluded.lng,
                    primary_type    = COALESCE(excluded.primary_type, restaurants.primary_type),
                    last_scraped_at = excluded.last_scraped_at
                """,
                {
                    "name": r.get("name"),
                    "address": r.get("address"),
                    "place_id": r["place_id"],
                    "website_url": r.get("website_url"),
                    "lat": r.get("lat"),
                    "lng": r.get("lng"),
                    "primary_type": r.get("primary_type"),
                    "last_scraped_at": r.get("last_scraped_at"),
                },
            )
    return len(rows), len(rows)


def count_restaurants(db_path: str | None = None) -> int:
    with connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]


def list_restaurants(db_path: str | None = None) -> list[dict]:
    """Return all restaurants as plain dicts, newest-scraped first.

    Includes has_menu_text: whether a menu-text source has been ingested.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.name, r.address, r.place_id, r.website_url,
                   r.lat, r.lng, r.last_scraped_at, r.consumer_hidden,
                   r.refresh_enabled,
                   r.serves_vegetarian, r.price_level, r.primary_type,
                   r.editorial_summary, r.rating, r.user_rating_count,
                   r.open_now, r.opening_hours, r.hours_enriched_at,
                   r.enriched_at, r.business_status,
                   (
                       SELECT MAX(s.fetched_at) FROM sources s
                       WHERE s.restaurant_id = r.id AND s.type = 'text'
                         AND (s.url IS NULL OR s.url != 'google:editorial_summary')
                   ) AS menu_fetched_at,
                   (
                       SELECT SUM(LENGTH(s.content)) FROM sources s
                       WHERE s.restaurant_id = r.id AND s.type = 'text'
                         AND (s.url IS NULL OR s.url != 'google:editorial_summary')
                   ) AS menu_chars,
                   (
                       SELECT MAX(c.created_at)
                       FROM classifications c
                       JOIN dishes d ON d.id = c.dish_id
                       WHERE d.restaurant_id = r.id
                   ) AS last_classified_at,
                   r.last_classify_cost, r.last_classify_provider,
                   r.last_classified_hash, r.archived,
                   EXISTS (
                       SELECT 1 FROM sources s
                       WHERE s.restaurant_id = r.id AND s.type = 'text'
                         AND (s.url IS NULL OR s.url != 'google:editorial_summary')
                   ) AS has_menu_text,
                   (SELECT COUNT(*) FROM restaurant_votes v
                     WHERE v.restaurant_id = r.id AND v.vote = 'up') AS up_votes,
                   (SELECT COUNT(*) FROM restaurant_votes v
                     WHERE v.restaurant_id = r.id AND v.vote = 'down') AS down_votes,
                   cp.consecutive_failures AS crawl_failures,
                   cp.last_error AS crawl_last_error
            FROM restaurants r
            LEFT JOIN crawl_profiles cp ON cp.restaurant_id = r.id
            ORDER BY r.last_scraped_at DESC, r.name ASC
            """
        ).fetchall()
    out = [dict(r) for r in rows]
    for row in out:
        row["opening_hours"] = _decode_json_list(row.get("opening_hours"))
    return out


def update_food_signals(
    restaurant_id: int,
    *,
    serves_vegetarian: bool | None,
    price_level: str | None,
    primary_type: str | None,
    editorial_summary: str | None,
    rating: float | None,
    user_rating_count: int | None,
    open_now: bool | None,
    opening_hours: list[str],
    enriched_at: str,
    business_status: str | None = None,
    db_path: str | None = None,
) -> None:
    """Store Google-sourced structured food signals on a restaurant."""
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE restaurants SET
                serves_vegetarian = :veg,
                price_level       = :price,
                primary_type      = :ptype,
                editorial_summary = :editorial,
                rating            = :rating,
                user_rating_count = :rating_count,
                open_now          = :open_now,
                opening_hours     = :opening_hours,
                hours_enriched_at = :enriched,
                enriched_at       = :enriched,
                business_status   = :business_status
            WHERE id = :id
            """,
            {
                "id": restaurant_id,
                # store bool as 1/0, leave NULL when unknown
                "veg": None if serves_vegetarian is None else int(serves_vegetarian),
                "price": price_level,
                "ptype": primary_type,
                "editorial": editorial_summary,
                "rating": rating,
                "rating_count": user_rating_count,
                "open_now": None if open_now is None else int(open_now),
                "opening_hours": json.dumps(opening_hours),
                "enriched": enriched_at,
                "business_status": business_status,
            },
        )


def record_classify_cost(
    restaurant_id: int,
    cost: float | None,
    provider: str | None = None,
    db_path: str | None = None,
) -> None:
    """Remember the provider and API cost of the last classification run."""
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE restaurants
            SET last_classify_cost = ?, last_classify_provider = ?
            WHERE id = ?
            """,
            (None if cost is None else round(cost, 3), provider, restaurant_id),
        )


def set_restaurant_archived(
    restaurant_id: int, archived: bool, db_path: str | None = None
) -> bool:
    """Archive/restore a listing. Archived rows keep all their data but
    leave the Admin working set, consumer views, and bulk pipeline runs."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE restaurants SET archived = ? WHERE id = ?",
            (int(archived), restaurant_id),
        )
    return cur.rowcount > 0


def set_restaurant_hidden(
    restaurant_id: int, hidden: bool, db_path: str | None = None
) -> bool:
    """Manually hide/show a listing in consumer views without deleting data."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE restaurants SET consumer_hidden = ? WHERE id = ?",
            (int(hidden), restaurant_id),
        )
    return cur.rowcount > 0


def set_restaurant_refresh_enabled(
    restaurant_id: int, enabled: bool, db_path: str | None = None
) -> bool:
    """Include/exclude a restaurant from bulk scrape/classify refresh jobs."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE restaurants SET refresh_enabled = ? WHERE id = ?",
            (int(enabled), restaurant_id),
        )
    return cur.rowcount > 0


def upsert_menu_text(
    restaurant_id: int,
    url: str,
    content: str,
    fetched_at: str,
    db_path: str | None = None,
) -> None:
    """Insert or replace the menu-text source for (restaurant_id, url)."""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sources (restaurant_id, dish_id, type, content, url, fetched_at)
            VALUES (:restaurant_id, NULL, 'text', :content, :url, :fetched_at)
            ON CONFLICT (restaurant_id, url) WHERE restaurant_id IS NOT NULL
            DO UPDATE SET content = excluded.content, fetched_at = excluded.fetched_at
            """,
            {
                "restaurant_id": restaurant_id,
                "url": url,
                "content": content,
                "fetched_at": fetched_at,
            },
        )


def replace_menu_texts(
    restaurant_id: int,
    pages: list[tuple[str, str]],
    fetched_at: str,
    db_path: str | None = None,
) -> None:
    """Fresh snapshot of a restaurant's menu-text sources, one row per page.

    Upserts each kept (url, content) page, then prunes text sources from
    earlier scrapes whose URL was not kept this time — otherwise a site whose
    menu moved keeps feeding its old page to classification forever. Pruned
    rows may be referenced by classifications from a previous run; those links
    are nulled (the ingest → reclassify flow re-establishes them).
    """
    with connect(db_path) as conn:
        for url, content in pages:
            conn.execute(
                """
                INSERT INTO sources (restaurant_id, dish_id, type, content, url, fetched_at)
                VALUES (:restaurant_id, NULL, 'text', :content, :url, :fetched_at)
                ON CONFLICT (restaurant_id, url) WHERE restaurant_id IS NOT NULL
                DO UPDATE SET content = excluded.content, fetched_at = excluded.fetched_at
                """,
                {
                    "restaurant_id": restaurant_id,
                    "url": url,
                    "content": content,
                    "fetched_at": fetched_at,
                },
            )
        placeholders = ",".join("?" for _ in pages) or "''"
        stale_predicate = f"""
            restaurant_id = ? AND type = 'text'
            AND (url IS NULL OR url != 'google:editorial_summary')
            AND (url IS NULL OR url NOT IN ({placeholders}))
        """
        params = [restaurant_id, *[u for u, _ in pages]]
        conn.execute(
            f"""
            UPDATE classifications SET source_id = NULL
            WHERE source_id IN (SELECT id FROM sources WHERE {stale_predicate})
            """,
            params,
        )
        conn.execute(f"DELETE FROM sources WHERE {stale_predicate}", params)


def get_crawl_profile(
    restaurant_id: int, db_path: str | None = None
) -> dict | None:
    """Return the crawler's last learned successful route for a restaurant."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM crawl_profiles WHERE restaurant_id = ?",
            (restaurant_id,),
        ).fetchone()
    if row is None:
        return None
    profile = dict(row)
    profile["menu_urls"] = _decode_json_list(profile.get("menu_urls"))
    return profile


def record_crawl_success(
    restaurant_id: int,
    *,
    menu_urls: list[str],
    crawl_method: str,
    content_hash: str,
    menu_score: float,
    char_count: int,
    crawled_at: str | None = None,
    db_path: str | None = None,
) -> None:
    """Learn a validated crawl route, replacing an older/stale profile."""
    timestamp = crawled_at or datetime.now(timezone.utc).isoformat()
    urls = list(dict.fromkeys(url for url in menu_urls if url))
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO crawl_profiles
                (restaurant_id, menu_urls, crawl_method, content_hash,
                 menu_score, char_count, last_attempt_at, last_success_at,
                 consecutive_failures, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            ON CONFLICT (restaurant_id) DO UPDATE SET
                menu_urls = excluded.menu_urls,
                crawl_method = excluded.crawl_method,
                content_hash = excluded.content_hash,
                menu_score = excluded.menu_score,
                char_count = excluded.char_count,
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                consecutive_failures = 0,
                last_error = NULL
            """,
            (
                restaurant_id,
                json.dumps(urls),
                crawl_method,
                content_hash,
                float(menu_score),
                int(char_count),
                timestamp,
                timestamp,
            ),
        )


def record_crawl_failure(
    restaurant_id: int,
    error: str,
    *,
    attempted_at: str | None = None,
    diagnostics: list | None = None,
    db_path: str | None = None,
) -> None:
    """Record a failed attempt without discarding the last successful route.

    diagnostics (the scraper's per-URL trail: what was fetched, how it
    scored, why it was rejected) is persisted so the Admin panel can show
    WHY the menu failed, not just that it did.
    """
    timestamp = attempted_at or datetime.now(timezone.utc).isoformat()
    diag_json = json.dumps(diagnostics[:40]) if diagnostics else None
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO crawl_profiles
                (restaurant_id, menu_urls, last_attempt_at,
                 consecutive_failures, last_error, last_diagnostics)
            VALUES (?, '[]', ?, 1, ?, ?)
            ON CONFLICT (restaurant_id) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                consecutive_failures = crawl_profiles.consecutive_failures + 1,
                last_error = excluded.last_error,
                last_diagnostics = excluded.last_diagnostics
            """,
            (
                restaurant_id,
                timestamp,
                (error or "unknown crawl failure")[:1000],
                diag_json,
            ),
        )


def scrape_failures(db_path: str | None = None) -> list[dict]:
    """Restaurants whose LAST scrape attempt failed, with the evidence:
    error, attempt counts, and the per-URL diagnostics trail."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.name, r.website_url, r.archived,
                   p.last_error, p.consecutive_failures,
                   p.last_attempt_at, p.last_success_at, p.last_diagnostics
            FROM crawl_profiles p
            JOIN restaurants r ON r.id = p.restaurant_id
            WHERE p.consecutive_failures > 0 AND r.archived = 0
            ORDER BY p.last_attempt_at DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        try:
            entry["diagnostics"] = json.loads(entry.pop("last_diagnostics") or "[]")
        except (TypeError, json.JSONDecodeError):
            entry["diagnostics"] = []
        out.append(entry)
    return out


def record_menu_version(
    restaurant_id: int,
    content: str,
    content_hash: str,
    *,
    menu_score: float | None = None,
    char_count: int | None = None,
    fetched_at: str | None = None,
    db_path: str | None = None,
) -> bool:
    """Store a menu capture as an immutable version; True when it's new.

    Identical recrawls (same fingerprint) don't add rows, so the table reads
    as "every time this menu actually changed".
    """
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO menu_versions
                (restaurant_id, content_hash, content, menu_score,
                 char_count, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                restaurant_id,
                content_hash,
                content,
                menu_score,
                char_count if char_count is not None else len(content),
                timestamp,
            ),
        )
    return cur.rowcount > 0


def list_menu_versions(
    restaurant_id: int, *, include_content: bool = False, db_path: str | None = None
) -> list[dict]:
    """A restaurant's distinct menu captures, newest first."""
    columns = "id, restaurant_id, content_hash, menu_score, char_count, fetched_at"
    if include_content:
        columns += ", content"
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT {columns} FROM menu_versions
            WHERE restaurant_id = ?
            ORDER BY fetched_at DESC, id DESC
            """,
            (restaurant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def snapshot_dishes(restaurant_id: int, db_path: str | None = None) -> dict[str, dict]:
    """Current dishes keyed by name with price + latest verdict — the
    'before' picture for computing dish changes across a reclassification."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.name, d.price, c.verdict
            FROM dishes d
            LEFT JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            WHERE d.restaurant_id = ?
            """,
            (restaurant_id,),
        ).fetchall()
    return {r["name"]: {"price": r["price"], "verdict": r["verdict"]} for r in rows}


def compute_dish_changes(
    prior: dict[str, dict], current: dict[str, dict]
) -> list[dict]:
    """Diff two dish snapshots ({name: {price, verdict}}) into change rows."""
    changes: list[dict] = []
    for name, now_ in current.items():
        before = prior.get(name)
        if before is None:
            changes.append(
                {
                    "change_type": "added",
                    "dish_name": name,
                    "new_price": now_.get("price"),
                    "new_verdict": now_.get("verdict"),
                }
            )
            continue
        if (before.get("price") or None) != (now_.get("price") or None):
            changes.append(
                {
                    "change_type": "price_changed",
                    "dish_name": name,
                    "old_price": before.get("price"),
                    "new_price": now_.get("price"),
                    "old_verdict": before.get("verdict"),
                    "new_verdict": now_.get("verdict"),
                }
            )
        elif (before.get("verdict") or None) != (now_.get("verdict") or None):
            changes.append(
                {
                    "change_type": "verdict_changed",
                    "dish_name": name,
                    "old_price": before.get("price"),
                    "new_price": now_.get("price"),
                    "old_verdict": before.get("verdict"),
                    "new_verdict": now_.get("verdict"),
                }
            )
    for name, before in prior.items():
        if name not in current:
            changes.append(
                {
                    "change_type": "removed",
                    "dish_name": name,
                    "old_price": before.get("price"),
                    "old_verdict": before.get("verdict"),
                }
            )
    return changes


def record_dish_changes(
    restaurant_id: int,
    changes: list[dict],
    observed_at: str | None = None,
    db_path: str | None = None,
) -> None:
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO dish_changes
                (restaurant_id, observed_at, change_type, dish_name,
                 old_price, new_price, old_verdict, new_verdict)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    restaurant_id,
                    timestamp,
                    c["change_type"],
                    c["dish_name"],
                    c.get("old_price"),
                    c.get("new_price"),
                    c.get("old_verdict"),
                    c.get("new_verdict"),
                )
                for c in changes
            ],
        )


def list_dish_changes(
    restaurant_id: int, *, limit: int = 200, db_path: str | None = None
) -> list[dict]:
    """A restaurant's dish-change history, newest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT observed_at, change_type, dish_name,
                   old_price, new_price, old_verdict, new_verdict
            FROM dish_changes
            WHERE restaurant_id = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            (restaurant_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_dish(restaurant_id: int, name: str, db_path: str | None = None) -> bool:
    """Surgically remove one dish (delta reclassification's 'removed' path).

    Same care as delete_dishes_for_restaurant: reports keep their dish_name
    but drop the FK link; classifications go with the dish.
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM dishes WHERE restaurant_id = ? AND name = ?",
            (restaurant_id, name),
        ).fetchone()
        if row is None:
            return False
        dish_id = row["id"]
        conn.execute("UPDATE reports SET dish_id = NULL WHERE dish_id = ?", (dish_id,))
        conn.execute("DELETE FROM classifications WHERE dish_id = ?", (dish_id,))
        conn.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
    return True


def set_last_classified_hash(
    restaurant_id: int, content_hash: str | None, db_path: str | None = None
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE restaurants SET last_classified_hash = ? WHERE id = ?",
            (content_hash, restaurant_id),
        )


def get_menu_text(restaurant_id: int, db_path: str | None = None) -> dict | None:
    """Return the stored menu text for a restaurant, or None.

    A restaurant's menu may span several stored pages (breakfast/lunch/dinner
    each a source row); they are combined with [page: url] headers so callers
    (classification, Admin menu view) always see the whole menu. `id`/`url`
    are the first page's — the scraper stores the best-scoring page first —
    keeping classification source links valid.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, restaurant_id, content, url, fetched_at
            FROM sources
            WHERE restaurant_id = ? AND type = 'text'
              AND (url IS NULL OR url != 'google:editorial_summary')
            ORDER BY id ASC
            """,
            (restaurant_id,),
        ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return dict(rows[0])
    combined = dict(rows[0])
    combined["content"] = "\n\n".join(
        f"[page: {r['url']}]\n{r['content']}" for r in rows
    )
    combined["fetched_at"] = max(r["fetched_at"] for r in rows)
    combined["page_count"] = len(rows)
    return combined


def upsert_dish(
    restaurant_id: int,
    name: str,
    raw_description: str | None,
    price: str | None,
    category: str | None = None,
    calories: str | None = None,
    db_path: str | None = None,
) -> int:
    """Insert/update a dish using conservative canonical identity."""
    with connect(db_path) as conn:
        identity = dish_identity_key(name, price, raw_description, calories)
        existing_rows = conn.execute(
            """
            SELECT id, name, raw_description, price, calories
            FROM dishes WHERE restaurant_id = ?
            """,
            (restaurant_id,),
        ).fetchall()
        existing = next(
            (
                row
                for row in existing_rows
                if dish_identity_key(
                    row["name"], row["price"], row["raw_description"], row["calories"]
                ) == identity
            ),
            None,
        )
        if existing is not None:
            display_name = preferred_dish_name(existing["name"], name)
            conn.execute(
                """
                UPDATE dishes
                SET name = ?, raw_description = ?, price = ?, calories = ?, category = ?
                WHERE id = ?
                """,
                (
                    display_name,
                    raw_description,
                    price,
                    calories,
                    category,
                    existing["id"],
                ),
            )
            return existing["id"]
        row = conn.execute(
            """
            INSERT INTO dishes
                (restaurant_id, name, raw_description, price, calories, category)
            VALUES (:rid, :name, :descr, :price, :calories, :category)
            ON CONFLICT (restaurant_id, name) DO UPDATE SET
                raw_description = excluded.raw_description,
                price           = excluded.price,
                calories        = excluded.calories,
                category        = excluded.category
            RETURNING id
            """,
            {
                "rid": restaurant_id,
                "name": name,
                "descr": raw_description,
                "price": price,
                "calories": calories,
                "category": category,
            },
        ).fetchone()
    return row[0]


def deduplicate_dishes_for_restaurant(
    restaurant_id: int, db_path: str | None = None
) -> list[dict]:
    """Merge safe formatting duplicates and preserve all dependent records.

    Same-name items with different prices, descriptions, or calories remain
    separate. Returns a small audit trail of the rows that were merged.
    """
    merged: list[dict] = []
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, name, raw_description, price, calories, category
            FROM dishes WHERE restaurant_id = ? ORDER BY id
            """,
            (restaurant_id,),
        ).fetchall()
        groups: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            identity = dish_identity_key(
                row["name"], row["price"], row["raw_description"], row["calories"]
            )
            groups.setdefault(identity, []).append(row)

        for group in groups.values():
            if len(group) < 2:
                continue
            preferred_name = group[0]["name"]
            for row in group[1:]:
                preferred_name = preferred_dish_name(preferred_name, row["name"])
            survivor = next(
                (row for row in group if row["name"] == preferred_name), group[0]
            )
            duplicate_ids = [row["id"] for row in group if row["id"] != survivor["id"]]
            for duplicate_id in duplicate_ids:
                conn.execute(
                    "UPDATE classifications SET dish_id = ? WHERE dish_id = ?",
                    (survivor["id"], duplicate_id),
                )
                conn.execute(
                    "UPDATE sources SET dish_id = ? WHERE dish_id = ?",
                    (survivor["id"], duplicate_id),
                )
                conn.execute(
                    "UPDATE dish_votes SET dish_id = ?, dish_name = ? WHERE dish_id = ?",
                    (survivor["id"], preferred_name, duplicate_id),
                )
                conn.execute(
                    "UPDATE reports SET dish_id = ?, dish_name = ? WHERE dish_id = ?",
                    (survivor["id"], preferred_name, duplicate_id),
                )
                conn.execute("DELETE FROM dishes WHERE id = ?", (duplicate_id,))
            conn.execute(
                "UPDATE dishes SET name = ? WHERE id = ?",
                (preferred_name, survivor["id"]),
            )
            conn.execute(
                "UPDATE dish_votes SET dish_name = ? WHERE dish_id = ?",
                (preferred_name, survivor["id"]),
            )
            conn.execute(
                "UPDATE reports SET dish_name = ? WHERE dish_id = ?",
                (preferred_name, survivor["id"]),
            )
            merged.append(
                {
                    "survivor_id": survivor["id"],
                    "name": preferred_name,
                    "removed_ids": duplicate_ids,
                }
            )
    return merged


def insert_classification(
    dish_id: int,
    verdict: str,
    confidence: float,
    reasoning: str,
    source_id: int | None,
    model_version: str,
    created_at: str,
    dairy_status: str = "unclear",
    gluten_status: str = "unclear",
    nut_status: str = "unclear",
    protein_level: str = "unclear",
    serving_role: str = "unclear",
    meal_types: list[str] | None = None,
    key_ingredients: list[str] | None = None,
    alcohol_status: str = "unclear",
    db_path: str | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO classifications
                (dish_id, verdict, confidence, reasoning, source_id,
                 model_version, created_at, dairy_status, gluten_status,
                 nut_status, protein_level, serving_role, meal_types,
                 key_ingredients, alcohol_status)
            VALUES (:dish_id, :verdict, :confidence, :reasoning, :source_id,
                    :model_version, :created_at, :dairy_status, :gluten_status,
                    :nut_status, :protein_level, :serving_role, :meal_types,
                    :key_ingredients, :alcohol_status)
            """,
            {
                "dish_id": dish_id,
                "verdict": verdict,
                "confidence": confidence,
                "reasoning": reasoning,
                "source_id": source_id,
                "model_version": model_version,
                "created_at": created_at,
                "dairy_status": dairy_status,
                "gluten_status": gluten_status,
                "nut_status": nut_status,
                "protein_level": protein_level,
                "serving_role": serving_role,
                "meal_types": json.dumps(meal_types or []),
                "key_ingredients": json.dumps(key_ingredients or []),
                "alcohol_status": alcohol_status,
            },
        )


def delete_dishes_for_restaurant(restaurant_id: int, db_path: str | None = None) -> int:
    """Remove a restaurant's dishes (and their classifications).

    Used before storing a fresh classification so dishes that left the menu
    don't linger with stale verdicts. Returns the number of dishes removed.
    """
    with connect(db_path) as conn:
        # Preserve reports while allowing the classified dish snapshot to be
        # replaced. dish_name is stored on the report for Admin context.
        conn.execute(
            """
            UPDATE reports SET dish_id = NULL
            WHERE dish_id IN (SELECT id FROM dishes WHERE restaurant_id = ?)
            """,
            (restaurant_id,),
        )
        conn.execute(
            """
            DELETE FROM classifications WHERE dish_id IN
                (SELECT id FROM dishes WHERE restaurant_id = ?)
            """,
            (restaurant_id,),
        )
        cur = conn.execute("DELETE FROM dishes WHERE restaurant_id = ?", (restaurant_id,))
    return cur.rowcount


def list_dishes(restaurant_id: int, db_path: str | None = None) -> list[dict]:
    """All dishes for a restaurant with their LATEST classification."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.name, d.raw_description, d.price, d.calories,
                   d.category,
                   c.verdict, c.confidence, c.reasoning, c.model_version,
                   c.created_at AS classified_at,
                   c.dairy_status, c.gluten_status, c.nut_status,
                   c.protein_level, c.serving_role, c.meal_types,
                   c.key_ingredients, c.alcohol_status,
                   (SELECT COUNT(*) FROM dish_votes v
                     WHERE v.dish_id = d.id AND v.vote = 'up') AS up_votes,
                   (SELECT COUNT(*) FROM dish_votes v
                     WHERE v.dish_id = d.id AND v.vote = 'down') AS down_votes,
                   s.url AS menu_url
            FROM dishes d
            LEFT JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            LEFT JOIN sources s ON s.id = c.source_id
            WHERE d.restaurant_id = ?
            ORDER BY
                CASE c.verdict
                    WHEN 'vegan' THEN 0
                    WHEN 'likely_vegan' THEN 1
                    WHEN 'vegan_adaptable' THEN 2
                    WHEN 'unclear' THEN 3
                    ELSE 4
                END,
                d.name ASC
            """,
            (restaurant_id,),
        ).fetchall()
    out = [dict(r) for r in rows]
    for row in out:
        row["meal_types"] = _decode_json_list(row.get("meal_types"))
        row["key_ingredients"] = _decode_json_list(row.get("key_ingredients"))
    return out


def list_all_dishes(db_path: str | None = None) -> list[dict]:
    """Every dish with its latest classification and restaurant context.

    This is the read model for cross-restaurant dish search. Keep search and
    filtering in the frontend for the local MVP so typing is instant and a
    single response can power result counts/facets without repeated queries.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.restaurant_id, d.name, d.raw_description,
                   d.price, d.calories, d.category,
                   c.verdict, c.confidence, c.reasoning, c.model_version,
                   c.created_at AS classified_at,
                   c.dairy_status, c.gluten_status, c.nut_status,
                   c.protein_level, c.serving_role, c.meal_types,
                   c.key_ingredients, c.alcohol_status,
                   r.name AS restaurant_name, r.address,
                   r.website_url, r.lat, r.lng, r.consumer_hidden,
                   r.archived,
                   r.serves_vegetarian,
                   r.price_level, r.primary_type, r.rating,
                   r.user_rating_count, r.open_now, r.opening_hours,
                   r.enriched_at,
                   (
                       SELECT MAX(ms.fetched_at) FROM sources ms
                       WHERE ms.restaurant_id = r.id AND ms.type = 'text'
                         AND (ms.url IS NULL OR ms.url != 'google:editorial_summary')
                   ) AS menu_fetched_at,
                   (SELECT COUNT(*) FROM dish_votes v
                     WHERE v.dish_id = d.id AND v.vote = 'up') AS up_votes,
                   (SELECT COUNT(*) FROM dish_votes v
                     WHERE v.dish_id = d.id AND v.vote = 'down') AS down_votes,
                   s.url AS menu_url
            FROM dishes d
            JOIN restaurants r ON r.id = d.restaurant_id
            LEFT JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            LEFT JOIN sources s ON s.id = c.source_id
            ORDER BY d.name COLLATE NOCASE ASC, r.name COLLATE NOCASE ASC
            """
        ).fetchall()
    out = [dict(r) for r in rows]
    for row in out:
        row["opening_hours"] = _decode_json_list(row.get("opening_hours"))
        row["meal_types"] = _decode_json_list(row.get("meal_types"))
        row["key_ingredients"] = _decode_json_list(row.get("key_ingredients"))
    return out


def _decode_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def restaurants_needing_refresh(
    stale_days: int = 30, db_path: str | None = None
) -> list[dict]:
    """Restaurants whose latest real menu source is older than stale_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, stale_days))
    targets = []
    for row in list_restaurants(db_path):
        fetched = row.get("menu_fetched_at")
        if not row.get("website_url") or not fetched:
            continue
        try:
            timestamp = datetime.fromisoformat(fetched).astimezone(timezone.utc)
        except ValueError:
            continue
        if timestamp < cutoff:
            targets.append(
                {"id": row["id"], "name": row["name"],
                 "website_url": row["website_url"],
                 "address": row.get("address")}
            )
    return targets


def record_audits(
    entries: Iterable[dict],
    *,
    provider: str,
    model: str | None = None,
    restaurant_id: int | None = None,
    db_path: str | None = None,
) -> int:
    """Persist guardrail flags / spot-check outcomes. Returns rows written."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            entry.get("restaurant_id", restaurant_id),
            entry.get("dish_name"),
            provider,
            entry.get("model", model),
            entry.get("check_type", "guardrail"),
            entry.get("rule"),
            entry.get("status", "flagged"),
            entry.get("detail"),
            entry.get("expected_verdict"),
            entry.get("actual_verdict"),
            entry.get("created_at", now),
        )
        for entry in entries
    ]
    if not rows:
        return 0
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO classification_audits
                (restaurant_id, dish_name, provider, model, check_type, rule,
                 status, detail, expected_verdict, actual_verdict, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def list_audits(
    limit: int = 100,
    provider: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    where = "WHERE provider = ?" if provider else ""
    params: tuple = (provider, limit) if provider else (limit,)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT a.*, r.name AS restaurant_name
            FROM classification_audits a
            LEFT JOIN restaurants r ON r.id = a.restaurant_id
            {where}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def audit_summary(db_path: str | None = None) -> dict:
    """Monitoring rollup: per provider, guardrail flag counts and spot-check
    agreement — the "can the cheap tier be trusted" dashboard numbers."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT provider, check_type, status, COUNT(*) AS n,
                   MAX(created_at) AS last_at
            FROM classification_audits
            GROUP BY provider, check_type, status
            """
        ).fetchall()
        corrections = conn.execute(
            "SELECT COUNT(*) FROM classifier_corrections WHERE active = 1"
        ).fetchone()[0]
    providers: dict[str, dict] = {}
    for row in rows:
        p = providers.setdefault(row["provider"], {
            "guardrail_flagged": 0, "guardrail_downgraded": 0,
            "spot_check_agree": 0, "spot_check_disagree": 0,
            "last_audit_at": None,
        })
        key = f"{row['check_type']}_{row['status']}"
        if key in p:
            p[key] += row["n"]
        p["last_audit_at"] = max(p["last_audit_at"] or "", row["last_at"] or "")
    for p in providers.values():
        checked = p["spot_check_agree"] + p["spot_check_disagree"]
        p["spot_check_agreement"] = (
            round(p["spot_check_agree"] / checked, 3) if checked else None
        )
    return {"providers": providers, "active_corrections": corrections}


def record_correction(
    dish_name: str,
    wrong_verdict: str,
    correct_verdict: str,
    *,
    description: str | None = None,
    note: str | None = None,
    source: str = "spot_check",
    db_path: str | None = None,
) -> None:
    """Store a learned correction; replaces an older one for the same dish so
    the guidance block stays current instead of accumulating duplicates."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE classifier_corrections SET active = 0 "
            "WHERE dish_name = ? COLLATE NOCASE AND active = 1",
            (dish_name,),
        )
        conn.execute(
            """
            INSERT INTO classifier_corrections
                (dish_name, description, wrong_verdict, correct_verdict,
                 note, source, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                dish_name, description, wrong_verdict, correct_verdict,
                note, source, datetime.now(timezone.utc).isoformat(),
            ),
        )


def list_corrections(
    active_only: bool = True,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict]:
    where = "WHERE active = 1" if active_only else ""
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM classifier_corrections
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def sample_recent_classifications(
    model_like: str,
    limit: int = 10,
    db_path: str | None = None,
) -> list[dict]:
    """Random sample of dishes whose LATEST classification came from a model
    matching model_like (SQL LIKE) — the spot-check auditor's input."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.id AS dish_id, d.name, d.raw_description, d.price,
                   d.restaurant_id, r.name AS restaurant_name,
                   r.primary_type,
                   c.verdict, c.confidence, c.model_version, c.created_at
            FROM dishes d
            JOIN restaurants r ON r.id = d.restaurant_id
            JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            WHERE c.model_version LIKE ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (model_like, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def record_restaurant_vote(
    restaurant_id: int,
    vote: str | None,
    client_id: str | None = None,
    db_path: str | None = None,
) -> bool:
    """Thumbs up/down on a restaurant, same one-live-vote-per-client rule as
    record_dish_vote. False when the restaurant is unknown."""
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM restaurants WHERE id = ?", (restaurant_id,)
        ).fetchone()
        if exists is None:
            return False
        if client_id:
            if vote is None:
                conn.execute(
                    "DELETE FROM restaurant_votes "
                    "WHERE restaurant_id = ? AND client_id = ?",
                    (restaurant_id, client_id),
                )
                return True
            updated = conn.execute(
                """
                UPDATE restaurant_votes SET vote = ?, created_at = ?
                WHERE restaurant_id = ? AND client_id = ?
                """,
                (vote, now, restaurant_id, client_id),
            )
            if updated.rowcount > 0:
                return True
        elif vote is None:
            return True  # nothing to withdraw without a client identity
        conn.execute(
            """
            INSERT INTO restaurant_votes (restaurant_id, vote, client_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (restaurant_id, vote, client_id, now),
        )
    return True


def record_dish_vote(
    dish_id: int,
    vote: str | None,
    client_id: str | None = None,
    db_path: str | None = None,
) -> bool:
    """Store a thumbs up/down for a dish; dish name/restaurant are copied so
    the signal survives dish re-snapshots. False when the dish is unknown.

    With a client_id, each browser holds at most ONE live vote per dish:
    voting again switches it, and vote=None withdraws it — so a visitor
    mashing the button can't inflate the count. Without a client_id (legacy
    callers), every call appends a row as before.
    """
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        dish = conn.execute(
            "SELECT name, restaurant_id FROM dishes WHERE id = ?", (dish_id,)
        ).fetchone()
        if dish is None:
            return False
        if client_id:
            if vote is None:
                conn.execute(
                    "DELETE FROM dish_votes WHERE dish_id = ? AND client_id = ?",
                    (dish_id, client_id),
                )
                return True
            updated = conn.execute(
                """
                UPDATE dish_votes SET vote = ?, created_at = ?
                WHERE dish_id = ? AND client_id = ?
                """,
                (vote, now, dish_id, client_id),
            )
            if updated.rowcount > 0:
                return True
        elif vote is None:
            return True  # nothing to withdraw without a client identity
        conn.execute(
            """
            INSERT INTO dish_votes
                (dish_id, dish_name, restaurant_id, vote, client_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                dish_id,
                dish["name"],
                dish["restaurant_id"],
                vote,
                client_id,
                now,
            ),
        )
    return True


def create_report(
    restaurant_id: int,
    issue_type: str,
    *,
    dish_id: int | None = None,
    note: str | None = None,
    db_path: str | None = None,
) -> int:
    """Store a user-submitted data-quality report and return its id."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            INSERT INTO reports
                (restaurant_id, dish_id, dish_name, issue_type, note, status, created_at)
            VALUES (?, ?, (SELECT name FROM dishes WHERE id = ?), ?, ?, 'open', ?)
            RETURNING id
            """,
            (
                restaurant_id,
                dish_id,
                dish_id,
                issue_type,
                note,
                datetime.now(timezone.utc).isoformat(),
            ),
        ).fetchone()
    return row[0]


def list_menu_quality_reviews(db_path: str | None = None) -> dict[int, dict]:
    """Latest human menu-audit disposition, keyed by restaurant id."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM menu_quality_reviews ORDER BY reviewed_at DESC"
        ).fetchall()
    return {row["restaurant_id"]: dict(row) for row in rows}


def set_menu_quality_review(
    restaurant_id: int,
    *,
    fingerprint: str,
    status: str,
    note: str | None = None,
    db_path: str | None = None,
) -> None:
    if status not in {"verified", "known_issue"}:
        raise ValueError("status must be verified or known_issue")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO menu_quality_reviews
                (restaurant_id, fingerprint, status, note, reviewed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (restaurant_id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                status = excluded.status,
                note = excluded.note,
                reviewed_at = excluded.reviewed_at
            """,
            (
                restaurant_id,
                fingerprint,
                status,
                (note or "").strip()[:1000] or None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def clear_menu_quality_review(
    restaurant_id: int, db_path: str | None = None
) -> bool:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM menu_quality_reviews WHERE restaurant_id = ?",
            (restaurant_id,),
        )
    return cursor.rowcount > 0


def list_dish_audit_reviews(db_path: str | None = None) -> dict[tuple[int, str], dict]:
    """Dismissed dish-audit findings, keyed by (dish_id, code)."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM dish_audit_reviews").fetchall()
    return {(row["dish_id"], row["code"]): dict(row) for row in rows}


def dismiss_dish_audit_finding(
    dish_id: int,
    *,
    code: str,
    fingerprint: str,
    note: str | None = None,
    db_path: str | None = None,
) -> None:
    """Mark one (dish, finding-code) as reviewed-and-fine at this value."""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO dish_audit_reviews
                (dish_id, code, fingerprint, note, reviewed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (dish_id, code) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                note = excluded.note,
                reviewed_at = excluded.reviewed_at
            """,
            (
                dish_id,
                code,
                fingerprint,
                (note or "").strip()[:500] or None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def update_dish_field(
    dish_id: int,
    *,
    field: str,
    value: str | None,
    db_path: str | None = None,
) -> bool:
    """Correct one scalar column on a dish (price or calories only).

    Used by the audit's one-click fixes (a lost-decimal price). Restricted to
    the two free-text fields an audit ever repairs — never touches verdicts
    or the classification, which stay the model's to own.
    """
    if field not in ("price", "calories"):
        raise ValueError("update_dish_field only edits price or calories")
    with connect(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE dishes SET {field} = ? WHERE id = ?",  # noqa: S608 (field allowlisted)
            (value, dish_id),
        )
    return cursor.rowcount > 0


def delete_restaurant(
    restaurant_id: int,
    *,
    expected_name: str,
    db_path: str | None = None,
) -> dict | None:
    """Permanently remove a restaurant and every dependent record.

    The exact name is checked inside the same transaction as the deletes. This
    is intentionally separate from archive/hide, both of which preserve data.
    """
    with connect(db_path) as conn:
        restaurant = conn.execute(
            "SELECT id, name FROM restaurants WHERE id = ?", (restaurant_id,)
        ).fetchone()
        if restaurant is None:
            return None
        if expected_name != restaurant["name"]:
            raise ValueError("Restaurant name confirmation does not match.")

        counts = {
            "dishes": conn.execute(
                "SELECT COUNT(*) FROM dishes WHERE restaurant_id = ?",
                (restaurant_id,),
            ).fetchone()[0],
            "sources": conn.execute(
                """
                SELECT COUNT(*) FROM sources
                WHERE restaurant_id = ? OR dish_id IN
                    (SELECT id FROM dishes WHERE restaurant_id = ?)
                """,
                (restaurant_id, restaurant_id),
            ).fetchone()[0],
            "classifications": conn.execute(
                """
                SELECT COUNT(*) FROM classifications WHERE dish_id IN
                    (SELECT id FROM dishes WHERE restaurant_id = ?)
                """,
                (restaurant_id,),
            ).fetchone()[0],
            "reports": conn.execute(
                "SELECT COUNT(*) FROM reports WHERE restaurant_id = ?",
                (restaurant_id,),
            ).fetchone()[0],
            "menu_versions": conn.execute(
                "SELECT COUNT(*) FROM menu_versions WHERE restaurant_id = ?",
                (restaurant_id,),
            ).fetchone()[0],
            "dish_changes": conn.execute(
                "SELECT COUNT(*) FROM dish_changes WHERE restaurant_id = ?",
                (restaurant_id,),
            ).fetchone()[0],
        }

        # Delete explicit non-cascading relationships first. Some sources are
        # restaurant-level; others attach directly to a dish.
        conn.execute("DELETE FROM reports WHERE restaurant_id = ?", (restaurant_id,))
        conn.execute(
            """
            DELETE FROM classifications WHERE dish_id IN
                (SELECT id FROM dishes WHERE restaurant_id = ?)
            """,
            (restaurant_id,),
        )
        conn.execute(
            """
            DELETE FROM sources
            WHERE restaurant_id = ? OR dish_id IN
                (SELECT id FROM dishes WHERE restaurant_id = ?)
            """,
            (restaurant_id, restaurant_id),
        )
        conn.execute("DELETE FROM dishes WHERE restaurant_id = ?", (restaurant_id,))
        conn.execute("DELETE FROM crawl_profiles WHERE restaurant_id = ?", (restaurant_id,))
        conn.execute("DELETE FROM menu_versions WHERE restaurant_id = ?", (restaurant_id,))
        conn.execute("DELETE FROM dish_changes WHERE restaurant_id = ?", (restaurant_id,))
        conn.execute(
            "DELETE FROM menu_quality_reviews WHERE restaurant_id = ?",
            (restaurant_id,),
        )
        conn.execute("DELETE FROM restaurants WHERE id = ?", (restaurant_id,))

    return {"id": restaurant_id, "name": restaurant["name"], **counts}


def list_reports(
    status: str | None = "open", db_path: str | None = None
) -> list[dict]:
    """Reports with restaurant/dish labels for the Admin review queue."""
    where = "WHERE p.status = ?" if status else ""
    params = (status,) if status else ()
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT p.id, p.restaurant_id, p.dish_id, p.issue_type, p.note,
                   p.status, p.created_at, p.resolved_at,
                   r.name AS restaurant_name, COALESCE(p.dish_name, d.name) AS dish_name
            FROM reports p
            JOIN restaurants r ON r.id = p.restaurant_id
            LEFT JOIN dishes d ON d.id = p.dish_id
            {where}
            ORDER BY p.created_at DESC, p.id DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_report(report_id: int, db_path: str | None = None) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE reports SET status = 'resolved', resolved_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (datetime.now(timezone.utc).isoformat(), report_id),
        )
    return cur.rowcount > 0


# What earns a spot in a headline "N vegan" count: a `vegan` verdict, or
# `likely_vegan` at/above this confidence. `vegan_adaptable` NEVER counts —
# a dish that needs modification isn't vegan as served, and calling it vegan
# on a card is exactly the confidently-wrong failure CLAUDE.md forbids.
# Mirrored in frontend/src/verdicts.js.
STRICT_LIKELY_VEGAN_MIN_CONFIDENCE = 0.75

# Venues whose PRODUCT is dessert: for them, vegan desserts ARE the vegan
# options, so the dessert exclusion below would zero out a perfectly
# vegan-friendly ice cream shop (Sampaguita: 7 vegan flavors, headline "no
# vegan meals"). Google primary_type values. Mirrored in
# frontend/src/cuisine.js (isDessertVenue) for the "vegan treats" label.
DESSERT_VENUE_TYPES = frozenset({
    "ice_cream_shop", "dessert_shop", "dessert_restaurant", "bakery",
    "donut_shop", "bagel_shop", "chocolate_shop", "chocolate_factory",
    "candy_store", "confectionery", "frozen_yogurt_shop", "acai_shop",
})


def verdict_counts_by_restaurant(db_path: str | None = None) -> dict[int, dict]:
    """Per restaurant: dish total (all), per-verdict maps over FOOD only
    (split meals vs sides), and STRICT vegan counts for headline display.

    Drinks and desserts are excluded from the verdict maps so "12 vegan
    meals" can't mean sodas or brownies, and sides are counted separately so
    it can't mean 12 bags of chips either — EXCEPT at dessert venues
    (DESSERT_VENUE_TYPES), where desserts are the product and count toward
    the headline number (the UI labels them "treats", not "meals").
    NULL category (pre-category rows) is treated as food; NULL/'unclear'
    serving_role (pre-role rows) is treated as a meal so the headline count
    doesn't collapse before dishes are re-classified.

    vegan_meals / vegan_sides apply the strict standard (see
    STRICT_LIKELY_VEGAN_MIN_CONFIDENCE); the by_verdict maps keep the full
    distribution for detail views.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.restaurant_id, c.verdict, r.primary_type,
                   (d.category IS NULL OR d.category = 'food') AS is_food,
                   (d.category = 'dessert') AS is_dessert,
                   (c.serving_role = 'side') AS is_side,
                   COUNT(*) AS n,
                   SUM(CASE
                       WHEN c.verdict = 'vegan' THEN 1
                       WHEN c.verdict = 'likely_vegan'
                            AND c.confidence >= :min_confidence THEN 1
                       ELSE 0
                   END) AS strict_n,
                   SUM(CASE
                       WHEN c.verdict = 'vegan'
                            OR (c.verdict = 'likely_vegan'
                                AND c.confidence >= :min_confidence)
                       THEN (CASE
                           -- how "filling" is this counted vegan dish:
                           -- protein-rich = fully; a dish the restaurant
                           -- NAMED vegan is purpose-built and nearly so
                           -- (Black Magic's Vegan Dr Pepperoni is a whole
                           -- pizza, not a salad); moderate protein partial;
                           -- anything else a sliver.
                           WHEN c.protein_level = 'high' THEN 1.0
                           WHEN LOWER(d.name) LIKE '%vegan%'
                                OR d.name LIKE '%Ⓥ%' THEN 0.9
                           WHEN c.protein_level = 'moderate' THEN 0.6
                           ELSE 0.1
                       END)
                       ELSE 0
                   END) AS strict_substance_points
            FROM dishes d
            JOIN restaurants r ON r.id = d.restaurant_id
            JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            GROUP BY d.restaurant_id, c.verdict, is_food, is_dessert, is_side
            """,
            {"min_confidence": STRICT_LIKELY_VEGAN_MIN_CONFIDENCE},
        ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        entry = out.setdefault(
            r["restaurant_id"],
            {
                "total": 0,
                "by_verdict": {},
                "sides_by_verdict": {},
                "vegan_meals": 0,
                "vegan_sides": 0,
                # weighted "how filling" points over the counted vegan
                # meals — feeds the Vegan Score's substance component (a
                # menu of plain salads is not the same as tofu bowls or a
                # purpose-built vegan pizza line).
                "vegan_substance_points": 0.0,
            },
        )
        entry["total"] += r["n"]
        dessert_venue_product = (
            r["is_dessert"] and r["primary_type"] in DESSERT_VENUE_TYPES
        )
        if r["is_food"] or dessert_venue_product:
            # A dessert venue's desserts are its PRODUCT — the classifier
            # tags scoops/slices serving_role 'side', but here they belong
            # in the headline count, never the sides bucket.
            is_side = r["is_side"] and not dessert_venue_product
            bucket = "sides_by_verdict" if is_side else "by_verdict"
            entry[bucket][r["verdict"]] = (
                entry[bucket].get(r["verdict"], 0) + r["n"]
            )
            entry["vegan_sides" if is_side else "vegan_meals"] += r["strict_n"]
            if not is_side:
                entry["vegan_substance_points"] += r["strict_substance_points"]
    return out


def restaurants_needing_classification(db_path: str | None = None) -> list[int]:
    """Restaurant ids that have real menu text but no classified dishes yet."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT s.restaurant_id
            FROM sources s
            WHERE s.type = 'text'
              AND (s.url IS NULL OR s.url != 'google:editorial_summary')
              AND NOT EXISTS (
                  SELECT 1 FROM dishes d WHERE d.restaurant_id = s.restaurant_id
              )
            """
        ).fetchall()
    return [r[0] for r in rows]


def restaurants_needing_ingest(db_path: str | None = None) -> list[dict]:
    """Restaurants that have a website but no menu-text source yet."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.name, r.website_url, r.address
            FROM restaurants r
            WHERE r.website_url IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM sources s
                  WHERE s.restaurant_id = r.id AND s.type = 'text'
                    AND (s.url IS NULL OR s.url != 'google:editorial_summary')
              )
            ORDER BY r.name ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]
