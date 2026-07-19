"""Consumer-facing venue eligibility shared by discovery and API reads."""

EXCLUDED_PRIMARY_TYPES = {
    "convenience_store",
    "gas_station",
    "grocery_store",
    "supermarket",
    "liquor_store",
    "pharmacy",
    "store",
    "shopping_mall",
    # A pet bakery/groomer (Woof Gang Bakery) matches a "bakery" sweep but is
    # not a food venue for humans.
    "pet_care",
}


# Primary types that count as human food venues when hydrating free ID-sweep
# results. The sweep's free-text queries surface loosely related businesses
# (a car-wrap shop matching "wraps", print kiosks inside food halls), so
# hydration needs an ALLOWLIST — the exclusion list above only knows the
# common Nearby Search offenders. Any "*_restaurant" type also qualifies.
FOOD_PRIMARY_TYPES = {
    "acai_shop", "bagel_shop", "bakery", "bar", "bar_and_grill",
    "beer_garden", "brewery", "brewpub", "cafe", "cafeteria", "cake_shop",
    "cocktail_bar", "coffee_roastery", "coffee_shop", "coffee_stand", "deli",
    "dessert_shop", "diner", "donut_shop", "food_court", "food_stand",
    "gastropub", "ice_cream_shop", "juice_shop", "meal_delivery",
    "meal_takeaway", "pastry_shop", "pub", "sandwich_shop", "snack_bar",
    "sports_bar", "tea_house", "wine_bar", "winery",
}


def is_food_primary_type(primary_type: str | None) -> bool:
    """Food-venue check by type allowlist; unknown types stay visible."""
    if not primary_type:
        return True
    return (
        primary_type == "restaurant"
        or primary_type.endswith("_restaurant")
        or primary_type in FOOD_PRIMARY_TYPES
    )


def is_consumer_food_venue(place: dict) -> bool:
    """Whether a Places row belongs in consumer restaurant/food views.

    Missing types remain visible because older/mock discovery records only get
    a primary type during enrichment. Admin always retains every row.
    """
    return (
        not bool(place.get("archived"))
        and not bool(place.get("consumer_hidden"))
        and place.get("primary_type") not in EXCLUDED_PRIMARY_TYPES
    )


def is_consumer_ready(place: dict, dish_count: int | None) -> bool:
    """Show a consumer venue only after it has classified dishes.

    Admin still sees every pipeline row. Explore and static exports begin
    showing a venue automatically once classification successfully persists
    at least one dish.
    """
    return is_consumer_food_venue(place) and bool((dish_count or 0) > 0)
