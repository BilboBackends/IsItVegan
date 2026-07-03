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
    created_at    TEXT
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
    },
    "dishes": {
        # food | drink | dessert — drinks are excluded from the headline
        # "vegan options" count (a list of vegan sodas isn't the product).
        "category": "TEXT",
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
                   r.last_classify_cost,
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
    restaurant_id: int, cost: float, db_path: str | None = None
) -> None:
    """Remember what the last classification run actually cost ($)."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE restaurants SET last_classify_cost = ? WHERE id = ?",
            (round(cost, 3), restaurant_id),
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
    db_path: str | None = None,
) -> int:
    """Insert or update a dish keyed on (restaurant_id, name); return dish id."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            INSERT INTO dishes (restaurant_id, name, raw_description, price, category)
            VALUES (:rid, :name, :descr, :price, :category)
            ON CONFLICT (restaurant_id, name) DO UPDATE SET
                raw_description = excluded.raw_description,
                price           = excluded.price,
                category        = excluded.category
            RETURNING id
            """,
            {
                "rid": restaurant_id,
                "name": name,
                "descr": raw_description,
                "price": price,
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
    db_path: str | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO classifications
                (dish_id, verdict, confidence, reasoning, source_id,
                 model_version, created_at)
            VALUES (:dish_id, :verdict, :confidence, :reasoning, :source_id,
                    :model_version, :created_at)
            """,
            {
                "dish_id": dish_id,
                "verdict": verdict,
                "confidence": confidence,
                "reasoning": reasoning,
                "source_id": source_id,
                "model_version": model_version,
                "created_at": created_at,
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
            SELECT d.id, d.name, d.raw_description, d.price, d.category,
                   c.verdict, c.confidence, c.reasoning, c.model_version,
                   c.created_at AS classified_at
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
    return [dict(r) for r in rows]


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
                   d.price, d.category,
                   c.verdict, c.confidence, c.reasoning, c.model_version,
                   c.created_at AS classified_at,
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


def verdict_counts_by_restaurant(db_path: str | None = None) -> dict[int, dict]:
    """Per restaurant: dish total (all) + per-verdict counts over FOOD only.

    Drinks are excluded from by_verdict so "12 vegan options" can't mean
    12 sodas. NULL category (pre-category rows) is treated as food.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.restaurant_id, c.verdict,
                   (d.category IS NULL OR d.category != 'drink') AS is_food,
                   COUNT(*) AS n
            FROM dishes d
            JOIN classifications c ON c.id = (
                SELECT id FROM classifications
                WHERE dish_id = d.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            )
            GROUP BY d.restaurant_id, c.verdict, is_food
            """
        ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        entry = out.setdefault(
            r["restaurant_id"], {"total": 0, "by_verdict": {}}
        )
        entry["total"] += r["n"]
        if r["is_food"]:
            entry["by_verdict"][r["verdict"]] = (
                entry["by_verdict"].get(r["verdict"], 0) + r["n"]
            )
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
