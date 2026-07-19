from __future__ import annotations

import db


def test_restaurant_dish_read_includes_classification_source_url(tmp_path):
    path = str(tmp_path / "source-url.db")
    db.init_db(path)
    db.upsert_restaurants(
        [
            {
                "name": "Source Cafe",
                "place_id": "source-cafe",
                "website_url": "https://restaurant.example",
            }
        ],
        path,
    )
    restaurant_id = db.list_restaurants(path)[0]["id"]
    menu_url = "https://restaurant.example/menu"
    db.replace_menu_texts(
        restaurant_id,
        [(menu_url, "Tofu Bowl\ntofu, rice, vegetables\n$14")],
        fetched_at="2026-07-10T12:00:00+00:00",
        db_path=path,
    )
    source_id = db.get_menu_text(restaurant_id, db_path=path)["id"]
    dish_id = db.upsert_dish(
        restaurant_id,
        "Tofu Bowl",
        "tofu, rice, vegetables",
        "$14",
        db_path=path,
    )
    db.insert_classification(
        dish_id=dish_id,
        verdict="vegan",
        confidence=0.95,
        reasoning="The listed ingredients are plant-based.",
        source_id=source_id,
        model_version="test",
        created_at="2026-07-10T12:00:00+00:00",
        db_path=path,
    )

    dish = db.list_dishes(restaurant_id, path)[0]

    assert dish["menu_url"] == menu_url


def _mini_db(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO restaurants (id, name, address, place_id) "
            "VALUES (1, 'Domu', 'a', 'p1')"
        )
    return path


def test_price_format_and_missing_description_no_longer_duplicate(tmp_path):
    # Domu recapture: "$15.50 VEGAN + desc" then "$15.5 Vegan" without desc
    # must update the same row, not add a twin.
    path = _mini_db(tmp_path)
    a = db.upsert_dish(1, "VEGAN", "TOMATO + SHIITAKE BROTH", "$15.50",
                       db_path=path)
    b = db.upsert_dish(1, "Vegan", None, "$15.5", db_path=path)
    assert a == b
    with db.connect(path) as conn:
        row = conn.execute("SELECT name, raw_description FROM dishes").fetchone()
    # Kept the richer description; Title Case preferred over shouting.
    assert row["name"] == "Vegan"
    assert row["raw_description"] == "TOMATO + SHIITAKE BROTH"
    # Conflicting descriptions still make distinct dishes (menu variants).
    c = db.upsert_dish(1, "VEGAN", "COCONUT BROTH VERSION", "$15.5",
                       db_path=path)
    assert c != a


def test_dedupe_dishes_merges_and_repoints(tmp_path):
    path = _mini_db(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO dishes (id, restaurant_id, name, price, raw_description) "
                     "VALUES (10, 1, 'VEGAN', '$15.50', 'BROTH')")
        conn.execute("INSERT INTO dishes (id, restaurant_id, name, price, raw_description) "
                     "VALUES (11, 1, 'Vegan', '$15.5', NULL)")
        conn.execute("INSERT INTO dishes (id, restaurant_id, name, price) "
                     "VALUES (12, 1, 'Miso Ramen', '$14')")
        conn.execute("INSERT INTO classifications (dish_id, verdict, confidence, reasoning) "
                     "VALUES (10, 'vegan', 0.9, 'old')")
        conn.execute("INSERT INTO classifications (dish_id, verdict, confidence, reasoning) "
                     "VALUES (11, 'vegan', 0.95, 'new')")
        conn.execute("INSERT INTO dish_votes (dish_id, client_id, vote, created_at) VALUES (10, 'c1', 'up', 't')")
        conn.execute("INSERT INTO dish_votes (dish_id, client_id, vote, created_at) VALUES (11, 'c1', 'up', 't')")
    result = db.dedupe_dishes(db_path=path)
    assert result == {"groups_merged": 1, "rows_removed": 1}
    with db.connect(path) as conn:
        dishes = conn.execute("SELECT id, name, raw_description FROM dishes ORDER BY id").fetchall()
        assert [d["id"] for d in dishes] == [11, 12]  # newest classification wins
        assert dishes[0]["raw_description"] == "BROTH"
        classifications = conn.execute(
            "SELECT COUNT(*) FROM classifications WHERE dish_id = 11").fetchone()[0]
        assert classifications == 2  # both repointed to the survivor
        votes = conn.execute("SELECT COUNT(*) FROM dish_votes").fetchone()[0]
        assert votes == 1  # colliding vote dropped, not double-counted
