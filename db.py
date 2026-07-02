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
    last_scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS dishes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    name            TEXT NOT NULL,
    raw_description TEXT,
    price           TEXT,
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


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist. Safe to run repeatedly."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


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
                   EXISTS (
                       SELECT 1 FROM sources s
                       WHERE s.restaurant_id = r.id AND s.type = 'text'
                   ) AS has_menu_text
            FROM restaurants r
            ORDER BY r.last_scraped_at DESC, r.name ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


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
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (restaurant_id,),
        ).fetchone()
    return dict(row) if row else None


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
              )
            ORDER BY r.name ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]
