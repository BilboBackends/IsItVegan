from __future__ import annotations

import gzip
import json

import publish_static


def test_restaurant_dish_shards_are_grouped_and_stale_files_removed(
    tmp_path, monkeypatch
):
    shard_dir = tmp_path / "restaurant-dishes"
    shard_dir.mkdir()
    (shard_dir / "999.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(publish_static, "RESTAURANT_DISH_DIR", shard_dir)

    dishes = [
        {"id": 1, "restaurant_id": 10, "name": "Tofu Bowl"},
        {"id": 2, "restaurant_id": 10, "name": "Bean Taco"},
        {"id": 3, "restaurant_id": 20, "name": "Veggie Roll"},
    ]
    publish_static._write_restaurant_dish_shards(dishes, "2026-07-10T00:00:00Z")

    assert sorted(path.name for path in shard_dir.glob("*.json")) == [
        "10.json",
        "20.json",
    ]
    first = json.loads((shard_dir / "10.json").read_text(encoding="utf-8"))
    assert first["count"] == 2
    assert [dish["name"] for dish in first["dishes"]] == [
        "Tofu Bowl",
        "Bean Taco",
    ]


def test_json_snapshot_writes_compact_deterministic_gzip_copy(tmp_path):
    path = tmp_path / "dishes.json"
    payload = {"count": 1, "dishes": [{"id": 1, "name": "Crème brûlée"}]}

    publish_static._write_json_snapshot(path, payload, gzip_copy=True)
    first_compressed = (tmp_path / "dishes.json.gz").read_bytes()
    publish_static._write_json_snapshot(path, payload, gzip_copy=True)

    assert b'"count":1' in path.read_bytes()
    assert b'": ' not in path.read_bytes()
    assert gzip.decompress(first_compressed).decode("utf-8") == path.read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "dishes.json.gz").read_bytes() == first_compressed


def test_targeted_export_replaces_only_an_already_published_restaurant(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    shard_dir = data_dir / "restaurant-dishes"
    shard_dir.mkdir(parents=True)
    (data_dir / "restaurants.json").write_text(
        json.dumps(
            {
                "count": 2,
                "restaurants": [
                    {"id": 1, "name": "Keep Me"},
                    {"id": 2, "name": "Old Target", "dish_count": 1},
                ],
                "published_at": "old",
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "dishes.json").write_text(
        json.dumps(
            {
                "count": 2,
                "dishes": [
                    {"id": 10, "restaurant_id": 1, "name": "Keep Dish"},
                    {"id": 20, "restaurant_id": 2, "name": "Old Dish"},
                ],
                "published_at": "old",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(publish_static, "DATA_DIR", data_dir)
    monkeypatch.setattr(publish_static, "RESTAURANT_DISH_DIR", shard_dir)
    monkeypatch.setattr(publish_static.db, "init_db", lambda: None)
    monkeypatch.setattr(
        publish_static.db,
        "list_restaurants",
        lambda: [{"id": 2, "name": "New Target"}],
    )
    monkeypatch.setattr(
        publish_static.db,
        "verdict_counts_by_restaurant",
        lambda: {2: {"total": 2}},
    )
    monkeypatch.setattr(publish_static, "is_consumer_ready", lambda *_args: True)
    monkeypatch.setattr(
        publish_static,
        "_consumer_restaurant_row",
        lambda restaurant, _counts: {
            "id": restaurant["id"], "name": restaurant["name"], "dish_count": 2
        },
    )
    monkeypatch.setattr(
        publish_static.db,
        "list_all_dishes",
        lambda: [
            {
                "id": 21, "restaurant_id": 2, "name": "New A",
                "restaurant_name": "New Target", "verdict": "vegan",
            },
            {
                "id": 22, "restaurant_id": 2, "name": "New B",
                "restaurant_name": "New Target", "verdict": "not_vegan",
            },
            {
                "id": 30, "restaurant_id": 3, "name": "Unpublished",
                "restaurant_name": "Do Not Add", "verdict": "vegan",
            },
        ],
    )
    monkeypatch.setattr(
        publish_static, "is_consumer_food_venue", lambda _dish: True
    )

    summary = publish_static.export_restaurant(2)

    restaurants = json.loads(
        (data_dir / "restaurants.json").read_text(encoding="utf-8")
    )
    dishes = json.loads((data_dir / "dishes.json").read_text(encoding="utf-8"))
    shard = json.loads((shard_dir / "2.json").read_text(encoding="utf-8"))
    assert summary["restaurant_dishes"] == 2
    assert [row["id"] for row in restaurants["restaurants"]] == [1, 2]
    assert restaurants["restaurants"][1]["name"] == "New Target"
    assert [dish["id"] for dish in dishes["dishes"]] == [10, 21, 22]
    assert shard["count"] == 2
