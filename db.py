"""SQLite persistence layer.

Schema mirrors the data model in CLAUDE.md. SQLite is the MVP store; the
schema is kept plain (no SQLite-only features) so migrating to Postgres later
is a config change, not a rewrite.

Only the `restaurants` table is populated in Phase 0, but all tables are
created up front so the schema is stable across later stages.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
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
    -- Structured food signals from Google Places (New) Place Details.
    -- Nullable: Google doesn't populate these for every restaurant.
    serves_vegetarian INTEGER,   -- 1 / 0 / NULL (unknown)
    price_level       TEXT,      -- e.g. PRICE_LEVEL_MODERATE
    primary_type      TEXT,      -- e.g. thai_restaurant
    editorial_summary TEXT,      -- Google's short blurb (often names dishes)
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
        "serves_vegetarian": "INTEGER",
        "price_level": "TEXT",
        "primary_type": "TEXT",
        "editorial_summary": "TEXT",
        "enriched_at": "TEXT",
    },
    "dishes": {
        # food | drink | dessert — drinks are excluded from the headline
        # "vegan options" count (a list of vegan sodas isn't the product).
        "category": "TEXT",
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
                    (name, address, place_id, website_url, lat, lng, last_scraped_at)
                VALUES
                    (:name, :address, :place_id, :website_url, :lat, :lng, :last_scraped_at)
                ON CONFLICT(place_id) DO UPDATE SET
                    name            = excluded.name,
                    address         = excluded.address,
                    website_url     = excluded.website_url,
                    lat             = excluded.lat,
                    lng             = excluded.lng,
                    last_scraped_at = excluded.last_scraped_at
                """,
                {
                    "name": r.get("name"),
                    "address": r.get("address"),
                    "place_id": r["place_id"],
                    "website_url": r.get("website_url"),
                    "lat": r.get("lat"),
                    "lng": r.get("lng"),
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
                   r.lat, r.lng, r.last_scraped_at,
                   r.serves_vegetarian, r.price_level, r.primary_type,
                   r.editorial_summary, r.enriched_at,
                   EXISTS (
                       SELECT 1 FROM sources s
                       WHERE s.restaurant_id = r.id AND s.type = 'text'
                         AND (s.url IS NULL OR s.url != 'google:editorial_summary')
                   ) AS has_menu_text
            FROM restaurants r
            ORDER BY r.last_scraped_at DESC, r.name ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def update_food_signals(
    restaurant_id: int,
    *,
    serves_vegetarian: bool | None,
    price_level: str | None,
    primary_type: str | None,
    editorial_summary: str | None,
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
                "enriched": enriched_at,
            },
        )


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


def get_menu_text(restaurant_id: int, db_path: str | None = None) -> dict | None:
    """Return the stored menu-text source for a restaurant, or None."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, restaurant_id, content, url, fetched_at
            FROM sources
            WHERE restaurant_id = ? AND type = 'text'
              AND (url IS NULL OR url != 'google:editorial_summary')
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (restaurant_id,),
        ).fetchone()
    return dict(row) if row else None


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
