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
