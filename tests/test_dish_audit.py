"""Tier-0 dish audit: deterministic field/consistency checks (no tokens).

Each test pins a real signature the audit must catch or must NOT flag —
calibrated against production data where $900 whisky, 2840-cal wings, and
$275 catering platters are all legitimate, so magnitude alone means nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import dish_audit  # noqa: E402


def _dish(**overrides):
    base = {
        "id": 1,
        "restaurant_id": 1,
        "name": "Some Dish",
        "price": None,
        "calories": None,
        "category": "food",
        "verdict": "vegan",
        "dairy_status": "free",
        "serving_role": "meal",
    }
    base.update(overrides)
    return base


# ---- number parsing --------------------------------------------------------

def test_price_value_takes_base_of_range_or_size():
    assert dish_audit.price_value("$99–$149") == 99.0
    assert dish_audit.price_value("8oz 4.25; 12oz 5.50") == 4.25
    assert dish_audit.price_value("from $10.99") == 10.99
    assert dish_audit.price_value("$110/lb") == 110.0
    assert dish_audit.price_value(None) is None
    assert dish_audit.price_value("market price") is None


def test_calorie_value_takes_top_of_range():
    assert dish_audit.calorie_value("cal 580") == 580
    assert dish_audit.calorie_value("1000-cal 1990") == 1990
    assert dish_audit.calorie_value("2220 cal") == 2220


# ---- lost-decimal price (the one auto-fixable case) ------------------------

def test_lost_decimal_price_is_flagged_with_correction():
    dishes = [_dish(name="Dinner Rolls", price=".99", category="food")]
    findings = dish_audit._audit_prices(dishes)
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "price_lost_decimal"
    assert f.current == ".99"
    assert f.suggested == "$0.99"


def test_normal_cents_price_without_dollars_is_not_flagged():
    # A genuine $0.99 stored as '0.99' or '$0.99' is fine — only a bare
    # cents string that parsed to WHOLE dollars (>=10) is the misparse.
    for good in ("$0.99", "0.99", "$6", "from $2.19"):
        assert dish_audit._audit_prices([_dish(price=good)]) == []


def test_expensive_legit_items_are_never_flagged():
    # Real data: none of these are errors, so a magnitude rule would be noise.
    for price in ("$900", "$275.00", "$600", "$149", "$120.00"):
        assert dish_audit._audit_prices([_dish(price=price)]) == []


# ---- calorie plausibility --------------------------------------------------

def test_calorie_high_for_side_is_flagged():
    dishes = [_dish(name="Cheese Fries", calories="2200 cal", serving_role="side")]
    findings = dish_audit._audit_calories(dishes)
    assert [f.code for f in findings] == ["calorie_high"]


def test_high_calorie_meal_within_reason_is_not_flagged():
    # 2840-cal jumbo wings are real; a meal ceiling well above that stays quiet.
    dishes = [_dish(name="Jumbo Wings", calories="cal 2840", serving_role="meal")]
    assert dish_audit._audit_calories(dishes) == []


def test_calorie_low_flagged():
    dishes = [_dish(name="Onions", calories="1 cal")]
    assert [f.code for f in dish_audit._audit_calories(dishes)] == ["calorie_low"]


# ---- verdict/attribute self-contradiction ----------------------------------

def test_vegan_verdict_with_contains_dairy_is_flagged():
    dishes = [_dish(name="Guacamole", verdict="vegan", dairy_status="contains")]
    findings = dish_audit._audit_consistency(dishes)
    assert [f.code for f in findings] == ["verdict_vs_dairy"]
    assert findings[0].severity == "high"


def test_not_vegan_with_dairy_free_is_not_flagged():
    # Meat with no dairy is legitimately dairy-free AND not_vegan.
    dishes = [_dish(name="Steak", verdict="not_vegan", dairy_status="free")]
    assert dish_audit._audit_consistency(dishes) == []


# ---- fingerprint stability -------------------------------------------------

def test_fingerprint_changes_when_value_changes():
    f1 = dish_audit.Finding(1, 1, "X", "calorie_high", "medium", "m",
                            field_name="calories", current="2200 cal")
    f2 = dish_audit.Finding(1, 1, "X", "calorie_high", "medium", "m",
                            field_name="calories", current="900 cal")
    assert f1.fingerprint() != f2.fingerprint()
    # Same value -> same fingerprint (a dismissal must stick).
    f3 = dish_audit.Finding(1, 1, "X", "calorie_high", "medium", "m",
                            field_name="calories", current="2200 cal")
    assert f1.fingerprint() == f3.fingerprint()


# ---- db round-trips --------------------------------------------------------

def test_dismiss_and_field_correction_roundtrip(tmp_path):
    path = str(tmp_path / "audit.db")
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO restaurants (id, name, place_id) VALUES (1, 'R', 'p1')"
        )
        conn.execute(
            "INSERT INTO dishes (id, restaurant_id, name, price) "
            "VALUES (5, 1, 'Dinner Rolls', '.99')"
        )

    # Field correction is restricted and it actually writes.
    assert db.update_dish_field(5, field="price", value="$0.99", db_path=path)
    with db.connect(path) as conn:
        assert conn.execute("SELECT price FROM dishes WHERE id=5").fetchone()[0] == "$0.99"

    # Off-allowlist fields are rejected outright.
    try:
        db.update_dish_field(5, field="verdict", value="vegan", db_path=path)
        assert False, "should have raised"
    except ValueError:
        pass

    # Dismissal persists and is keyed by (dish, code, fingerprint).
    db.dismiss_dish_audit_finding(5, code="calorie_high", fingerprint="abc123", db_path=path)
    reviews = db.list_dish_audit_reviews(path)
    assert reviews[(5, "calorie_high")]["fingerprint"] == "abc123"
