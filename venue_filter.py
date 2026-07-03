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
}


def is_consumer_food_venue(place: dict) -> bool:
    """Whether a Places row belongs in consumer restaurant/food views.

    Missing types remain visible because older/mock discovery records only get
    a primary type during enrichment. Admin always retains every row.
    """
    return (
        not bool(place.get("consumer_hidden"))
        and place.get("primary_type") not in EXCLUDED_PRIMARY_TYPES
    )
