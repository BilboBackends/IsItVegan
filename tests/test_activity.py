"""Admin activity feed: config gating and Supabase payload shaping."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import activity  # noqa: E402
import api  # noqa: E402


def _fake_settings(url, key):
    # config.Settings is a frozen dataclass; swap the module-level object.
    return SimpleNamespace(supabase_url=url, supabase_service_role_key=key)


def test_unconfigured_activity_reports_missing_variables(monkeypatch):
    monkeypatch.setattr(activity, "settings", _fake_settings(None, None))

    client = api.app.test_client()
    response = client.get("/api/admin/activity")

    assert response.status_code == 200
    data = response.get_json()
    assert data["enabled"] is False
    assert data["missing"] == ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]
    assert data["users"] == []
    assert data["comments"] == []


def test_activity_attaches_usernames_restaurants_and_reply_context(monkeypatch):
    monkeypatch.setattr(
        activity,
        "settings",
        _fake_settings("https://example.supabase.co", "service-key"),
    )

    monkeypatch.setattr(
        activity,
        "_auth_users",
        lambda client: [
            {
                "id": "u1",
                "email": "ada@example.com",
                "created_at": "2026-07-10T12:00:00Z",
                "last_sign_in_at": "2026-07-12T09:00:00Z",
                "app_metadata": {"provider": "google"},
            },
            {
                "id": "u2",
                "email": "sam@example.com",
                "created_at": "2026-07-11T15:00:00Z",
                "last_sign_in_at": None,
                "app_metadata": {"provider": "email"},
            },
        ],
    )

    tables = {
        "profiles": [
            {"id": "u1", "username": "ada", "display_name": "dish explorer"},
            {"id": "u2", "username": None, "display_name": "dish explorer"},
        ],
        "comments": [
            {
                "id": "c2",
                "user_id": "u2",
                "place_id": "place-1",
                "body": "@ada agreed!",
                "parent_comment_id": "c1",
                "created_at": "2026-07-12T10:00:00Z",
            },
            {
                "id": "c1",
                "user_id": "u1",
                "place_id": "place-1",
                "body": "The curry is fully vegan.",
                "parent_comment_id": None,
                "created_at": "2026-07-12T09:30:00Z",
            },
        ],
        "votes": [
            {
                "user_id": "u1",
                "kind": "dish",
                "place_id": "place-1",
                "dish_key": "vegetable-curry",
                "dish_name": "Vegetable Curry",
                "vote": "up",
                "updated_at": "2026-07-12T11:00:00Z",
            }
        ],
        "favorites": [],
        "comment_reports": [
            {"comment_id": "c1", "user_id": "u2",
             "created_at": "2026-07-12T12:00:00Z"},
        ],
    }
    monkeypatch.setattr(
        activity, "_rest", lambda client, table, params: tables[table]
    )
    monkeypatch.setattr(
        activity, "_place_names", lambda: {"place-1": "Curry House"}
    )

    data = activity.fetch_activity()

    assert data["enabled"] is True
    # Sign-ups newest first.
    assert [u["email"] for u in data["users"]] == [
        "sam@example.com", "ada@example.com",
    ]
    assert data["users"][1]["username"] == "ada"

    reply, original = data["comments"]
    assert reply["restaurant_name"] == "Curry House"
    assert reply["parent_username"] == "ada"  # reply context resolved
    assert original["parent_username"] is None
    assert data["votes"][0]["restaurant_name"] == "Curry House"
    assert data["reports"][0]["comment_body"] == "The curry is fully vegan."
