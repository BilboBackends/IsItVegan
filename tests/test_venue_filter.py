"""Consumer-venue eligibility, including the non-food types a gap sweep drags in."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from venue_filter import (
    EXCLUDED_PRIMARY_TYPES,
    is_consumer_food_venue,
    is_consumer_ready,
)


def test_real_restaurants_are_kept():
    for t in ("restaurant", "mexican_restaurant", "cafe", "bakery", "tea_house"):
        assert is_consumer_food_venue({"primary_type": t}), t


def test_non_food_venues_are_excluded():
    for t in ("shopping_mall", "liquor_store", "grocery_store", "gas_station"):
        assert not is_consumer_food_venue({"primary_type": t}), t


def test_pet_bakery_is_excluded():
    # Woof Gang Bakery (dog grooming) matches a "bakery" sweep but is not a
    # food venue for humans.
    assert "pet_care" in EXCLUDED_PRIMARY_TYPES
    assert not is_consumer_food_venue(
        {"name": "Woof Gang Bakery", "primary_type": "pet_care"}
    )


def test_missing_type_stays_visible():
    # Older/mock records only get a primary type at enrichment — don't hide them.
    assert is_consumer_food_venue({"name": "Some Spot"})


def test_archived_or_hidden_never_a_consumer_venue():
    assert not is_consumer_food_venue({"primary_type": "restaurant", "archived": 1})
    assert not is_consumer_food_venue(
        {"primary_type": "restaurant", "consumer_hidden": 1}
    )


def test_consumer_ready_requires_at_least_one_classified_dish():
    place = {"primary_type": "restaurant"}
    assert is_consumer_ready(place, 0) is False
    assert is_consumer_ready(place, None) is False
    assert is_consumer_ready(place, 1) is True
