"""User-activity feed for the Admin board.

Reads the Supabase user-data plane (accounts, comments, votes, favorites,
comment reports) with the SERVICE ROLE key, which bypasses Row Level
Security — that is exactly why this lives behind the local Flask backend
and never in any frontend bundle. The public anon key can only see
profiles(id, username), so sign-up times, emails, and reports are not
readable any other way.

Everything is returned as one JSON-ready dict; the Admin page merges the
lists into a single timeline client-side.
"""
from __future__ import annotations

from typing import Any

import httpx

import db
from config import settings

# One page of each activity kind is plenty for a monitoring view; the point
# is "what happened lately", not an archive browser.
FEED_LIMIT = 100
USERS_PER_PAGE = 200


def activity_config() -> dict[str, Any]:
    """Whether the activity feed can run, and what is missing if not."""
    missing = []
    if not settings.supabase_url:
        missing.append("SUPABASE_URL")
    if not settings.supabase_service_role_key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    return {"enabled": not missing, "missing": missing}


def _headers() -> dict[str, str]:
    key = settings.supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _rest(client: httpx.Client, table: str, params: dict[str, str]) -> list[dict]:
    response = client.get(
        f"{settings.supabase_url}/rest/v1/{table}",
        params=params,
        headers=_headers(),
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _auth_users(client: httpx.Client) -> list[dict]:
    """Accounts from the Auth admin API — the only source of email +
    last-sign-in. Trigger-created profile rows carry no login identity."""
    response = client.get(
        f"{settings.supabase_url}/auth/v1/admin/users",
        params={"page": "1", "per_page": str(USERS_PER_PAGE)},
        headers=_headers(),
    )
    response.raise_for_status()
    payload = response.json()
    users = payload.get("users") if isinstance(payload, dict) else payload
    return users if isinstance(users, list) else []


def _place_names() -> dict[str, str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT place_id, name FROM restaurants WHERE place_id IS NOT NULL"
        ).fetchall()
    return {row["place_id"]: row["name"] for row in rows}


def fetch_activity() -> dict[str, Any]:
    """Aggregate recent user activity; raises httpx.HTTPError on failure."""
    config = activity_config()
    if not config["enabled"]:
        return {**config, "users": [], "comments": [], "votes": [],
                "favorites": [], "reports": []}

    with httpx.Client(timeout=20) as client:
        users = _auth_users(client)
        profiles = _rest(client, "profiles", {
            "select": "id,username,display_name,created_at",
            "limit": "1000",
        })
        comments = _rest(client, "comments", {
            "select": "id,user_id,place_id,body,parent_comment_id,created_at",
            "order": "created_at.desc",
            "limit": str(FEED_LIMIT),
        })
        votes = _rest(client, "votes", {
            "select": "user_id,kind,place_id,dish_key,dish_name,vote,updated_at",
            "order": "updated_at.desc",
            "limit": str(FEED_LIMIT),
        })
        favorites = _rest(client, "favorites", {
            "select": "user_id,kind,place_id,dish_key,dish_name,created_at",
            "order": "created_at.desc",
            "limit": str(FEED_LIMIT),
        })
        reports = _rest(client, "comment_reports", {
            "select": "comment_id,user_id,created_at",
            "order": "created_at.desc",
            "limit": str(FEED_LIMIT),
        })

    usernames = {p["id"]: p.get("username") for p in profiles}
    place_names = _place_names()
    comment_authors = {c["id"]: c.get("user_id") for c in comments}
    comment_bodies = {c["id"]: c.get("body") for c in comments}

    def label(user_id: str | None) -> str | None:
        return usernames.get(user_id) if user_id else None

    out_users = [
        {
            "id": u.get("id"),
            "email": u.get("email"),
            "username": label(u.get("id")),
            "provider": (u.get("app_metadata") or {}).get("provider"),
            "created_at": u.get("created_at"),
            "last_sign_in_at": u.get("last_sign_in_at"),
        }
        for u in users
    ]
    out_users.sort(key=lambda u: u.get("created_at") or "", reverse=True)

    out_comments = [
        {
            **c,
            "username": label(c.get("user_id")),
            "restaurant_name": place_names.get(c.get("place_id")),
            # Reply context: who is being answered, when the parent is in
            # the same window. Older parents just show as replies.
            "parent_username": label(
                comment_authors.get(c.get("parent_comment_id"))
            ),
        }
        for c in comments
    ]
    out_votes = [
        {
            **v,
            "username": label(v.get("user_id")),
            "restaurant_name": place_names.get(v.get("place_id")),
        }
        for v in votes
    ]
    out_favorites = [
        {
            **f,
            "username": label(f.get("user_id")),
            "restaurant_name": place_names.get(f.get("place_id")),
        }
        for f in favorites
    ]
    out_reports = [
        {
            **r,
            "username": label(r.get("user_id")),
            "comment_body": comment_bodies.get(r.get("comment_id")),
        }
        for r in reports
    ]

    return {
        **config,
        "users": out_users,
        "comments": out_comments,
        "votes": out_votes,
        "favorites": out_favorites,
        "reports": out_reports,
    }
