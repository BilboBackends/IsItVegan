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
