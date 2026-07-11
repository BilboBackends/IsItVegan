"""Radius Prospect coverage, exact-boundary filtering, and API preflight."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api  # noqa: E402
import places_client as pc  # noqa: E402


def _raw(place_id: str, lat: float, lng: float, primary_type: str) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": place_id},
        "formattedAddress": "Orlando, FL",
        "location": {"latitude": lat, "longitude": lng},
        "primaryType": primary_type,
    }


def test_radius_grid_is_bounded_and_covers_small_circle():
    assert pc.radius_grid_centers(28.5, -81.4, 800, 1_000) == [(28.5, -81.4)]

    centers = pc.radius_grid_centers(28.5, -81.4, 5_000, 1_000)
    assert len(centers) > 1
    assert all(
        pc.distance_meters(28.5, -81.4, lat, lng) <= 6_000.1
        for lat, lng in centers
    )


def test_estimate_is_pure_and_exposes_cost_and_limit():
    estimate = pc.estimate_radius_food_sweep(28.5, -81.4, 5_000)
    assert estimate["base_calls"] == estimate["cells"] * 2
    assert estimate["call_budget"] == estimate["base_calls"]
    assert estimate["estimated_list_cost"] == round(
        estimate["base_calls"] * 0.035, 2
    )
    assert estimate["within_call_limit"] is True

    too_large = pc.estimate_radius_food_sweep(28.5, -81.4, 50_000)
    assert too_large["within_call_limit"] is False
    assert too_large["call_budget"] == pc.RADIUS_SWEEP_MAX_CALLS


def test_radius_sweep_searches_both_groups_and_filters_exact_circle(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_search(client, *, included_types, **kwargs):
        calls.append(included_types)
        if included_types == ("restaurant",):
            return [
                _raw("restaurant", 28.5005, -81.4, "restaurant"),
                _raw("outside", 28.52, -81.4, "restaurant"),
            ]
        return [
            _raw("brewery", 28.5007, -81.4, "brewery"),
            _raw("restaurant", 28.5005, -81.4, "restaurant"),
        ]

    monkeypatch.setattr(pc, "_search_circle", fake_search)
    estimate = pc.estimate_radius_food_sweep(28.5, -81.4, 500)
    result = pc.radius_food_sweep(
        api_key="fake",
        lat=28.5,
        lng=-81.4,
        radius_meters=500,
        confirmed_call_budget=estimate["call_budget"],
        workers=1,
    )

    assert len(calls) == 2
    assert {place["place_id"] for place in result["places"]} == {
        "restaurant",
        "brewery",
    }
    assert result["calls_run"] == 2
    assert result["places"][0]["distance_meters"] <= 500


def test_radius_sweep_rejects_stale_confirmation():
    estimate = pc.estimate_radius_food_sweep(28.5, -81.4, 500)
    try:
        pc.radius_food_sweep(
            api_key="fake",
            lat=28.5,
            lng=-81.4,
            radius_meters=500,
            confirmed_call_budget=estimate["call_budget"] + 1,
        )
    except ValueError as exc:
        assert "Estimate again" in str(exc)
    else:
        raise AssertionError("stale confirmation should be rejected")


def test_radius_estimate_endpoint_and_input_validation(monkeypatch):
    monkeypatch.setattr(
        api,
        "settings",
        dataclasses.replace(api.settings, google_places_api_key="fake"),
    )
    client = api.app.test_client()

    response = client.post(
        "/api/prospect/radius/estimate",
        json={"lat": 28.5, "lng": -81.4, "radius_meters": 1_000},
    )
    assert response.status_code == 200
    assert response.get_json()["base_calls"] == 2

    invalid = client.post(
        "/api/prospect/radius/estimate",
        json={"lat": True, "lng": -81.4, "radius_meters": 1_000},
    )
    assert invalid.status_code == 400


def test_confirmed_radius_endpoint_marks_new_places(monkeypatch):
    monkeypatch.setattr(
        api,
        "settings",
        dataclasses.replace(api.settings, google_places_api_key="fake"),
    )
    monkeypatch.setattr(api.db, "init_db", lambda: None)
    monkeypatch.setattr(api.db, "list_restaurants", lambda: [])
    monkeypatch.setattr(
        pc,
        "radius_food_sweep",
        lambda **kwargs: {
            "places": [
                {
                    "place_id": "brewery",
                    "name": "Brewery",
                    "address": "Orlando, FL",
                    "lat": 28.5,
                    "lng": -81.4,
                    "primary_type": "brewery",
                }
            ],
            "count": 1,
            "calls_run": 2,
            "cells": 1,
            "saturated_calls": 0,
            "errors": [],
        },
    )
    client = api.app.test_client()
    response = client.post(
        "/api/prospect/radius",
        json={
            "lat": 28.5,
            "lng": -81.4,
            "radius_meters": 500,
            "confirmed_call_budget": 2,
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["new_count"] == 1
    assert data["places"][0]["already_added_id"] is None
