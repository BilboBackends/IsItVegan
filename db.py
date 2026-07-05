"""SQLite persistence layer.

Schema mirrors the data model in CLAUDE.md. SQLite is the MVP store; the
schema is kept plain (no SQLite-only features) so migrating to Postgres later
is a config change, not a rewrite.

Only the `restaurants` table is populated in Phase 0, but all tables are
created up front so the schema is stable across later stages.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from config import settings

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
    },
    "reports": {
        "dish_name": "TEXT",
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
                   r.enriched_at,
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
                   r.last_classified_hash,
                   EXISTS (
                       SELECT 1 FROM sources s
                       WHERE s.restaurant_id = r.id AND s.type = 'text'
                         AND (s.url IS NULL OR s.url != 'google:editorial_summary')
                   ) AS has_menu_text
            FROM restaurants r
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
                enriched_at       = :enriched
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
    db_path: str | None = None,
) -> None:
    """Record a failed attempt without discarding the last successful route."""
    timestamp = attempted_at or datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO crawl_profiles
                (restaurant_id, menu_urls, last_attempt_at,
                 consecutive_failures, last_error)
            VALUES (?, '[]', ?, 1, ?)
            ON CONFLICT (restaurant_id) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                consecutive_failures = crawl_profiles.consecutive_failures + 1,
                last_error = excluded.last_error
            """,
            (restaurant_id, timestamp, (error or "unknown crawl failure")[:1000]),
        )


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
    """Insert or update a dish keyed on (restaurant_id, name); return dish id."""
    with connect(db_path) as conn:
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
    db_path: str | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO classifications
                (dish_id, verdict, confidence, reasoning, source_id,
                 model_version, created_at, dairy_status, gluten_status,
                 nut_status, protein_level, serving_role, meal_types,
                 key_ingredients)
            VALUES (:dish_id, :verdict, :confidence, :reasoning, :source_id,
                    :model_version, :created_at, :dairy_status, :gluten_status,
                    :nut_status, :protein_level, :serving_role, :meal_types,
                    :key_ingredients)
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
                   c.key_ingredients
            FROM dishes d
            LEFT JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
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
                   c.key_ingredients,
                   r.name AS restaurant_name, r.address,
                   r.website_url, r.lat, r.lng, r.consumer_hidden,
                   r.serves_vegetarian,
                   r.price_level, r.primary_type, r.rating,
                   r.user_rating_count, r.open_now, r.opening_hours,
                   r.enriched_at,
                   (
                       SELECT MAX(ms.fetched_at) FROM sources ms
                       WHERE ms.restaurant_id = r.id AND ms.type = 'text'
                         AND (ms.url IS NULL OR ms.url != 'google:editorial_summary')
                   ) AS menu_fetched_at,
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
                {"id": row["id"], "name": row["name"], "website_url": row["website_url"]}
            )
    return targets


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


def verdict_counts_by_restaurant(db_path: str | None = None) -> dict[int, dict]:
    """Per restaurant: dish total (all), per-verdict maps over FOOD only
    (split meals vs sides), and STRICT vegan counts for headline display.

    Drinks and desserts are excluded from the verdict maps so "12 vegan
    meals" can't mean sodas or brownies, and sides are counted separately so
    it can't mean 12 bags of chips either. NULL category (pre-category rows) is treated as food;
    NULL/'unclear' serving_role (pre-role rows) is treated as a meal so the
    headline count doesn't collapse before dishes are re-classified.

    vegan_meals / vegan_sides apply the strict standard (see
    STRICT_LIKELY_VEGAN_MIN_CONFIDENCE); the by_verdict maps keep the full
    distribution for detail views.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.restaurant_id, c.verdict,
                   (d.category IS NULL OR d.category = 'food') AS is_food,
                   (c.serving_role = 'side') AS is_side,
                   COUNT(*) AS n,
                   SUM(CASE
                       WHEN c.verdict = 'vegan' THEN 1
                       WHEN c.verdict = 'likely_vegan'
                            AND c.confidence >= :min_confidence THEN 1
                       ELSE 0
                   END) AS strict_n
            FROM dishes d
            JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            GROUP BY d.restaurant_id, c.verdict, is_food, is_side
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
            },
        )
        entry["total"] += r["n"]
        if r["is_food"]:
            bucket = "sides_by_verdict" if r["is_side"] else "by_verdict"
            entry[bucket][r["verdict"]] = (
                entry[bucket].get(r["verdict"], 0) + r["n"]
            )
            entry["vegan_sides" if r["is_side"] else "vegan_meals"] += r["strict_n"]
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
            SELECT r.id, r.name, r.website_url
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
