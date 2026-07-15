"""Regression coverage for bounded Admin menu/audit work."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api  # noqa: E402
import db  # noqa: E402
import menu_audit  # noqa: E402


def _database(tmp_path) -> str:
    path = str(tmp_path / "admin.db")
    db.init_db(path)
    with db.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO restaurants (id, name, address, place_id, website_url)
            VALUES (?, ?, 'Orlando, FL', ?, ?)
            """,
            [
                (1, "First Cafe", "first", "https://first.example"),
                (2, "Second Cafe", "second", "https://second.example"),
            ],
        )
    return path


def test_batched_menu_read_uses_one_connection_and_matches_single_read(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    db.replace_menu_texts(
        1,
        [
            ("https://first.example/lunch", "Lunch\nTofu bowl $12"),
            ("https://first.example/dinner", "Dinner\nPasta $16"),
        ],
        fetched_at="2026-07-14T12:00:00+00:00",
        db_path=path,
    )
    db.replace_menu_texts(
        2,
        [("https://second.example/menu", "Soup $8")],
        fetched_at="2026-07-14T12:00:00+00:00",
        db_path=path,
    )
    expected = db.get_menu_text(1, db_path=path)

    original_connect = db.connect
    connections = 0

    @contextmanager
    def counted_connect(db_path=None):
        nonlocal connections
        connections += 1
        with original_connect(db_path) as conn:
            yield conn

    monkeypatch.setattr(db, "connect", counted_connect)
    actual = db.get_menu_texts([1, 2], db_path=path)

    assert connections == 1
    assert actual[1]["content"] == expected["content"]
    assert actual[1]["page_count"] == 2
    assert actual[2]["content"] == "Soup $8"


def test_admin_menu_metrics_reuse_cache_and_invalidate_changed_source(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    db.replace_menu_texts(
        1,
        [("https://first.example/menu", "Salads\nGarden salad $9")],
        fetched_at="2026-07-14T12:00:00+00:00",
        db_path=path,
    )
    api._clear_admin_menu_metric_cache()
    original = db.get_menu_texts
    calls = 0

    def counted(ids, db_path=None):
        nonlocal calls
        calls += 1
        return original(ids, db_path=db_path)

    monkeypatch.setattr(api.db, "get_menu_texts", counted)
    rows = db.list_restaurants(path)
    first = api._admin_menu_metrics(rows, path)
    second = api._admin_menu_metrics(rows, path)

    assert calls == 1
    assert second == first
    assert first[1]["plant_protein_menu"] is False

    db.replace_menu_texts(
        1,
        [("https://first.example/menu", "Bowls\nTempeh bowl $14")],
        fetched_at="2026-07-14T12:01:00+00:00",
        db_path=path,
    )
    changed = api._admin_menu_metrics(db.list_restaurants(path), path)
    assert calls == 2
    assert changed[1]["plant_protein_menu"] is True


def test_admin_scores_exact_combined_menu_not_persisted_best_page(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    fetched_at = "2026-07-14T12:00:00+00:00"
    db.replace_menu_texts(
        1,
        [
            ("https://first.example/lunch", "Lunch\nTofu bowl $12"),
            ("https://first.example/dinner", "Dinner\nTempeh plate $18"),
        ],
        fetched_at=fetched_at,
        db_path=path,
    )
    db.record_crawl_success(
        1,
        menu_urls=[
            "https://first.example/lunch",
            "https://first.example/dinner",
        ],
        crawl_method="http",
        content_hash="current-fingerprint",
        # A crawl profile records the best individual page's score. It is not
        # interchangeable with the combined-menu score shown by Admin.
        menu_score=0.01,
        char_count=42,
        crawled_at=fetched_at,
        db_path=path,
    )
    api._clear_admin_menu_metric_cache()

    expected_content = db.get_menu_text(1, db_path=path)["content"]
    expected = api.score_menu_text(expected_content)
    original_score = api.score_menu_text
    scored_contents = []

    def counted_score(content):
        scored_contents.append(content)
        return original_score(content)

    monkeypatch.setattr(api, "score_menu_text", counted_score)
    metrics = api._admin_menu_metrics(db.list_restaurants(path), path)

    assert scored_contents == [expected_content]
    assert metrics[1]["menu_score"] == expected.score
    assert metrics[1]["menu_score_is_menu"] == expected.is_menu
    assert metrics[1]["menu_score_reason"] == expected.reason
    assert metrics[1]["menu_score"] != 0.01


def test_menu_audit_scores_combined_text_not_persisted_best_page(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    fetched_at = "2026-07-14T12:00:00+00:00"
    db.replace_menu_texts(
        1,
        [
            ("https://first.example/lunch", "Lunch\nTofu bowl $12"),
            ("https://first.example/dinner", "Dinner\nTempeh plate $18"),
        ],
        fetched_at=fetched_at,
        db_path=path,
    )
    db.record_crawl_success(
        1,
        menu_urls=[
            "https://first.example/lunch",
            "https://first.example/dinner",
        ],
        crawl_method="http",
        content_hash="current-fingerprint",
        menu_score=0.99,
        char_count=42,
        crawled_at=fetched_at,
        db_path=path,
    )
    scored_contents = []

    class WeakScore:
        score = 0.42

    def score_combined(content):
        scored_contents.append(content)
        return WeakScore()

    monkeypatch.setattr(menu_audit, "score_menu_text", score_combined)
    findings = menu_audit._audit_menus_uncached(path)
    first = next(
        finding for finding in findings if finding["restaurant_id"] == 1
    )

    assert scored_contents == ["Lunch\nTofu bowl $12\nDinner\nTempeh plate $18"]
    assert any("weak menu score (0.42)" in flag for flag in first["flags"])


def test_menu_audit_cache_reuses_unchanged_db_and_invalidates_write(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    menu_audit.clear_audit_cache(path)
    original = menu_audit._audit_menus_uncached
    calls = 0

    def counted(db_path=None):
        nonlocal calls
        calls += 1
        return original(db_path)

    monkeypatch.setattr(menu_audit, "_audit_menus_uncached", counted)
    first = menu_audit.audit_menus(path)
    second = menu_audit.audit_menus(path)
    assert first == second
    assert calls == 1

    with db.connect(path) as conn:
        conn.execute(
            "UPDATE restaurants SET website_url = ? WHERE id = 1",
            ("https://first.example/changed",),
        )
    menu_audit.audit_menus(path)
    assert calls == 2
