from __future__ import annotations

import gzip
import json

import publish_static


def test_global_dish_rows_keep_dish_data_and_drop_restaurant_context():
    dish = {
        "id": 7,
        "restaurant_id": 3,
        "name": "Tofu Bowl",
        "raw_description": "Tofu and greens",
        "reasoning": "No animal ingredients listed.",
        "menu_url": "https://example.com/menu",
        "model_version": "deepseek-v4",
        "classified_at": "2026-07-14T00:00:00Z",
        "key_ingredients": ["tofu", "greens"],
        "restaurant_name": "Repeated Cafe",
        "address": "1 Main St",
        "lat": 28.5,
        "opening_hours": ["Monday: 9:00 AM - 5:00 PM"],
        "rating": 4.7,
    }

    [compact] = publish_static._global_dish_rows([dish])

    assert compact == {
        "id": 7,
        "restaurant_id": 3,
        "name": "Tofu Bowl",
        "raw_description": "Tofu and greens",
        "reasoning": "No animal ingredients listed.",
        "menu_url": "https://example.com/menu",
        "key_ingredients": ["tofu", "greens"],
    }


def test_restaurants_receive_dish_id_locator_without_mutating_input():
    restaurants = [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]
    dishes = [
        {"id": 11, "restaurant_id": 1},
        {"id": 12, "restaurant_id": 1},
    ]

    located = publish_static._attach_dish_ids(restaurants, dishes)

    assert located[0]["dish_ids"] == [11, 12]
    assert located[1]["dish_ids"] == []
    assert "dish_ids" not in restaurants[0]


def test_compact_existing_snapshots_preserves_published_set_and_shards(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    shard_dir = data_dir / "restaurant-dishes"
    shard_dir.mkdir(parents=True)
    monkeypatch.setattr(publish_static, "DATA_DIR", data_dir)
    monkeypatch.setattr(publish_static, "RESTAURANT_DISH_DIR", shard_dir)
    (data_dir / "restaurants.json").write_text(
        json.dumps(
            {
                "restaurants": [
                    {"id": 1, "name": "Published"},
                    {"id": 2, "name": "Also Published"},
                ]
            }
        ),
        encoding="utf-8",
    )
    full_dishes = [
        {
            "id": 10,
            "restaurant_id": 1,
            "name": "Dish",
            "verdict": "vegan",
            "restaurant_name": "Repeated context",
            "address": "Repeated address",
        },
        {
            "id": 20,
            "restaurant_id": 2,
            "name": "Second dish",
            "verdict": "vegan",
            "restaurant_name": "Also Published",
            "address": "Second address",
        },
    ]
    legacy_payload = {"count": 2, "dishes": full_dishes, "published_at": "old"}
    publish_static._write_gzip_snapshot(
        data_dir / publish_static.LEGACY_DISH_GZIP_NAME,
        legacy_payload,
    )
    legacy_bytes = (
        data_dir / publish_static.LEGACY_DISH_GZIP_NAME
    ).read_bytes()
    shard_path = shard_dir / "1.json"
    shard_path.write_text(
        json.dumps({"count": 1, "dishes": [full_dishes[0]]}),
        encoding="utf-8",
    )
    second_shard_path = shard_dir / "2.json"
    second_shard_path.write_text(
        json.dumps({"count": 1, "dishes": [full_dishes[1]]}),
        encoding="utf-8",
    )
    original_shards = {
        shard_path: shard_path.read_bytes(),
        second_shard_path: second_shard_path.read_bytes(),
    }

    result = publish_static.compact_existing_snapshots()

    restaurants = json.loads(
        (data_dir / "restaurants.json").read_text(encoding="utf-8")
    )
    dishes = json.loads((data_dir / "dishes.json").read_text(encoding="utf-8"))
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["restaurants"] == 2
    assert result["dishes"] == 2
    assert [row["id"] for row in restaurants["restaurants"]] == [1, 2]
    assert restaurants["restaurants"][0]["dish_ids"] == [10]
    assert restaurants["restaurants"][1]["dish_ids"] == [20]
    assert "restaurant_name" not in dishes["dishes"][0]
    assert "address" not in dishes["dishes"][0]
    assert manifest["dish_count"] == 2
    assert manifest["dish_schema_version"] == 2
    assert (data_dir / publish_static.COMPACT_DISH_GZIP_NAME).exists()
    assert (data_dir / publish_static.LEGACY_DISH_GZIP_NAME).read_bytes() == legacy_bytes
    assert all(path.read_bytes() == content for path, content in original_shards.items())

    try:
        publish_static.compact_existing_snapshots()
    except RuntimeError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("migration should refuse to replay a frozen legacy catalog")


def test_compact_existing_snapshots_fails_before_writes_on_shard_mismatch(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    shard_dir = data_dir / "restaurant-dishes"
    shard_dir.mkdir(parents=True)
    monkeypatch.setattr(publish_static, "DATA_DIR", data_dir)
    monkeypatch.setattr(publish_static, "RESTAURANT_DISH_DIR", shard_dir)
    restaurants_path = data_dir / "restaurants.json"
    restaurants_bytes = b'{"count":1,"restaurants":[{"id":1,"name":"One"}]}'
    restaurants_path.write_bytes(restaurants_bytes)
    dish = {"id": 10, "restaurant_id": 1, "name": "Catalog dish"}
    publish_static._write_gzip_snapshot(
        data_dir / publish_static.LEGACY_DISH_GZIP_NAME,
        {"count": 1, "dishes": [dish]},
    )
    (shard_dir / "1.json").write_text(
        json.dumps({"count": 1, "dishes": [{**dish, "name": "Different"}]}),
        encoding="utf-8",
    )

    try:
        publish_static.compact_existing_snapshots()
    except RuntimeError as error:
        assert "does not exactly match" in str(error)
    else:
        raise AssertionError("mismatched menu shard should block migration")

    assert restaurants_path.read_bytes() == restaurants_bytes
    assert not (data_dir / publish_static.COMPACT_DISH_GZIP_NAME).exists()
    assert not (data_dir / "manifest.json").exists()


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
                    {
                        "id": 10,
                        "restaurant_id": 1,
                        "name": "Keep Dish",
                        "reasoning": "Keep this dish-level field",
                        "restaurant_name": "Keep Me",
                        "address": "Repeated context must disappear",
                    },
                    {
                        "id": 20,
                        "restaurant_id": 2,
                        "name": "Old Dish",
                        "restaurant_name": "Old Target",
                    },
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
    assert dishes["dishes"][0]["reasoning"] == "Keep this dish-level field"
    assert "restaurant_name" not in dishes["dishes"][0]
    assert "address" not in dishes["dishes"][0]
    assert shard["count"] == 2
    assert shard["dishes"][0]["restaurant_name"] == "New Target"
    assert restaurants["restaurants"][0]["dish_ids"] == [10]
    assert restaurants["restaurants"][1]["dish_ids"] == [21, 22]
    manifest = json.loads(
        (data_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["dish_count"] == 3
    assert manifest["dishes_version"] == dishes["published_at"]
