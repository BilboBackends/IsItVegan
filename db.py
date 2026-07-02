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

CREATE TABLE IF NOT EXISTS sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id    INTEGER NOT NULL REFERENCES dishes(id),
    type       TEXT NOT NULL CHECK (type IN ('text', 'image')),
    content    TEXT NOT NULL,
    fetched_at TEXT
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
