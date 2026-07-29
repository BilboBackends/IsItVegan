"""End-to-end tests for the rescrape -> reclassify cycle.

The unit tests in test_menu_history.py pin dish_identity in isolation. These
drive the real classify.run() persistence path against a temp DB with a
stubbed model, covering the three properties that matter on a recrawl:

1. correctness  — a rescrape pulls in the items actually on the new menu
2. no duplicates — cosmetic text drift (case, spacing, punctuation, unicode,
   trailing-zero prices, a gained/lost description) updates the existing row
   instead of inserting a second one
3. removals    — dishes that genuinely left the menu are deleted, in both
   full and delta mode, while a suspicious mass-removal delta is distrusted

Everything here is offline: classify_menu is monkeypatched, so no API calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import classify  # noqa: E402
import db  # noqa: E402
from classifier import ClassificationResult, ClassifiedDish  # noqa: E402
from config import settings  # noqa: E402


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    """Temp DB wired in as the process-wide database.

    classify.run() has no db_path parameter and config.settings is a frozen
    dataclass, so redirect at the single place the default is read:
    db.connect()'s `db_path or settings.database_path`.
    """
    path = str(tmp_path / "test.db")
    db.init_db(path)
    real_connect = db.connect

    def connect_to_tmp(db_path: str | None = None):
        return real_connect(db_path or path)

    monkeypatch.setattr(db, "connect", connect_to_tmp)
    return path


def _add_restaurant(path: str, rid: int = 1, name: str = "Test Kitchen") -> None:
    with db.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO restaurants (id, name, address, place_id, website_url)
            VALUES (?, ?, 'addr', ?, 'https://example.com')
            """,
            (rid, name, f"place_{rid}"),
        )


def _set_menu(path: str, rid: int, text: str) -> None:
    db.upsert_menu_text(
        rid,
        "https://example.com/menu",
        text,
        "2026-07-28T00:00:00+00:00",
        db_path=path,
    )


def _dish(name, price=None, description=None, calories=None, verdict="vegan"):
    return ClassifiedDish(
        name=name,
        description=description,
        price=price,
        calories=calories,
        verdict=verdict,
        confidence=0.9,
        reasoning="test",
        evidence=name,
        category="food",
    )


def _result(dishes, removed=None, mode="full"):
    return ClassificationResult(
        ok=True,
        dishes=dishes,
        removed_dish_names=list(removed or []),
        mode=mode,
    )


def _run_with(monkeypatch, path, results, mode="full", rid=1):
    """Drive classify.run(), returning each canned result in turn."""
    queue = list(results)
    captured: list[dict] = []

    def fake_classify_menu(menu_text, **kwargs):
        captured.append({"prior": kwargs.get("prior_dishes"), "text": menu_text})
        return queue.pop(0)

    monkeypatch.setattr(classify, "classify_menu", fake_classify_menu)
    classify.run(restaurant_id=rid, mode=mode, parallel=1)
    return captured


def _dish_rows(path: str, rid: int = 1) -> list[dict]:
    with db.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT name, price, raw_description, calories
            FROM dishes WHERE restaurant_id = ? ORDER BY name
            """,
            (rid,),
        ).fetchall()
    return [dict(r) for r in rows]


def _names(path: str, rid: int = 1) -> list[str]:
    return sorted(r["name"] for r in _dish_rows(path, rid))


# --------------------------------------------------------------------------
# 1. A rescrape pulls in the correct items
# --------------------------------------------------------------------------

def test_full_rescrape_stores_exactly_the_new_menu(test_db, monkeypatch):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "Hummus $6\nFalafel $8")
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("Hummus", "$6"), _dish("Falafel", "$8")])],
    )
    assert _names(test_db) == ["Falafel", "Hummus"]


def test_full_rescrape_adds_new_and_drops_departed(test_db, monkeypatch):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("Hummus", "$6"), _dish("Falafel", "$8")])],
    )
    # New menu: Falafel is gone, Tabbouleh is new, Hummus repriced.
    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("Hummus", "$7"), _dish("Tabbouleh", "$5")])],
    )
    assert _names(test_db) == ["Hummus", "Tabbouleh"]
    rows = {r["name"]: r for r in _dish_rows(test_db)}
    assert rows["Hummus"]["price"] == "$7"


def test_unchanged_menu_is_skipped_entirely(test_db, monkeypatch):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "Hummus $6\nFalafel $8")
    menu = db.get_menu_text(1, db_path=test_db)["content"]
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("Hummus", "$6"), _dish("Falafel", "$8")])],
    )
    db.set_last_classified_hash(1, classify._menu_hash(menu), db_path=test_db)

    def fake_classify_menu(menu_text, **kwargs):
        raise AssertionError("unchanged menu must not be re-classified")

    monkeypatch.setattr(classify, "classify_menu", fake_classify_menu)
    classify.run(restaurant_id=1, mode="auto", parallel=1)
    assert _names(test_db) == ["Falafel", "Hummus"]


# --------------------------------------------------------------------------
# 2. Cosmetic drift must not duplicate rows
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "first, second",
    [
        # (name, price, description) pairs that are the SAME dish
        (("Hummus Plate", "$6.50", "chickpeas"), ("HUMMUS PLATE", "$6.50", "chickpeas")),
        (("Hummus Plate", "$6.50", "chickpeas"), ("hummus  plate", "$6.50", "chickpeas")),
        (("Hummus Plate", "$6.50", "chickpeas"), ("Hummus-Plate!", "$6.50", "chickpeas")),
        # trailing-zero price drift (the Domu $15.50 / $15.5 case)
        (("Hummus Plate", "$15.50", "chickpeas"), ("Hummus Plate", "$15.5", "chickpeas")),
        (("Hummus Plate", "15.50", "chickpeas"), ("Hummus Plate", "$15.50", "chickpeas")),
        # description gained/lost between captures
        (("Hummus Plate", "$6.50", "chickpeas"), ("Hummus Plate", "$6.50", None)),
        (("Hummus Plate", "$6.50", None), ("Hummus Plate", "$6.50", "chickpeas")),
        # unicode NFKC drift: precomposed U+00E9 vs decomposed e + U+0301
        (("Café Latte", "$4", None), ("Café Latte", "$4", None)),
    ],
)
def test_cosmetic_drift_updates_instead_of_duplicating(test_db, monkeypatch, first, second):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    _run_with(monkeypatch, test_db, [_result([_dish(*first)])])
    assert len(_dish_rows(test_db)) == 1

    _set_menu(test_db, 1, "v2")
    _run_with(monkeypatch, test_db, [_result([_dish(*second)])])
    rows = _dish_rows(test_db)
    assert len(rows) == 1, f"{first} vs {second} duplicated: {rows}"


def test_genuine_variants_stay_separate_when_case_differs(test_db, monkeypatch):
    """Same-name variants survive when the menu's casing differs.

    This is the path test_menu_history.py covers ("Fries" vs "FRIES"): the
    names are not byte-identical, so UNIQUE(restaurant_id, name) doesn't fire
    and dish_identity's "different price/description = different dish" rule
    is honored.
    """
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    _run_with(
        monkeypatch,
        test_db,
        [
            _result(
                [
                    _dish("Pho", "$12", "small bowl"),
                    _dish("PHO", "$16", "large bowl"),
                ]
            )
        ],
    )
    assert len(_dish_rows(test_db)) == 2


def test_genuine_variants_stay_separate_with_identical_names(test_db, monkeypatch):
    """Same name, same casing, different price = two real dishes, not one.

    Pizza sizes, lunch/dinner portions and small/large bowls routinely repeat
    a name verbatim. Losing one is a wrong price shown to the user, and for a
    variant with a different preparation it can be a wrong verdict.
    """
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    _run_with(
        monkeypatch,
        test_db,
        [
            _result(
                [
                    _dish("Pho", "$12", "small bowl"),
                    _dish("Pho", "$16", "large bowl"),
                ]
            )
        ],
    )
    assert len(_dish_rows(test_db)) == 2


def test_price_variants_survive_a_full_recrawl_round_trip(test_db, monkeypatch):
    """Regression for the UNIQUE(restaurant_id, name) collapse.

    Modeled on Oda Pizzeria: one pizza name at two sizes/prices. Both rows
    must exist after the first pass AND still exist after a recrawl.
    """
    _add_restaurant(test_db)
    menu = [
        _dish("B.Y.O.Pie", "$15.00"),
        _dish("B.Y.O.Pie", "$18.00"),
        _dish("BBQ Chicken", "$21.00"),
        _dish("BBQ Chicken", "$25.20"),
    ]
    _set_menu(test_db, 1, "v1")
    _run_with(monkeypatch, test_db, [_result(menu)])
    assert len(_dish_rows(test_db)) == 4

    _set_menu(test_db, 1, "v2")
    _run_with(monkeypatch, test_db, [_result(menu)])
    rows = _dish_rows(test_db)
    assert len(rows) == 4
    assert {r["price"] for r in rows if r["name"] == "B.Y.O.Pie"} == {
        "$15.00",
        "$18.00",
    }


def test_price_variant_keeps_its_own_classification(test_db, monkeypatch):
    """Each variant must carry its own verdict, not share one row's."""
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    _run_with(
        monkeypatch,
        test_db,
        [
            _result(
                [
                    _dish("Combo", "$10", "veggie", verdict="vegan"),
                    _dish("Combo", "$14", "with pork", verdict="not_vegan"),
                ]
            )
        ],
    )
    with db.connect(test_db) as conn:
        pairs = conn.execute(
            """
            SELECT d.price, c.verdict
            FROM dishes d JOIN classifications c ON c.dish_id = d.id
            WHERE d.restaurant_id = 1 ORDER BY d.price
            """
        ).fetchall()
    assert [(r["price"], r["verdict"]) for r in pairs] == [
        ("$10", "vegan"),
        ("$14", "not_vegan"),
    ]


def test_delta_reprice_updates_in_place_instead_of_stacking(test_db, monkeypatch):
    """A price rise must move the row, not leave the old price beside it.

    Full mode wipes dishes first so it can't stack, but delta persists
    surgically — this is the mode where a stale price would survive.
    """
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00", f"descr {i}") for i in range(1, 7)]
    _run_with(monkeypatch, test_db, [_result(seed)])
    assert len(_dish_rows(test_db)) == 6

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("Dish 3", "$9.99", "descr 3")], mode="delta")],
        mode="auto",
    )
    rows = _dish_rows(test_db)
    assert len(rows) == 6, "reprice stacked a duplicate row"
    assert [r["price"] for r in rows if r["name"] == "Dish 3"] == ["$9.99"]


def test_full_mode_upserts_never_reprice(test_db):
    """Without allow_reprice a same-name/new-price dish is a NEW variant.

    Full mode clears the menu first, so every upsert is a fresh insert;
    treating one as a reprice there would silently destroy real size
    variants as the menu is rebuilt.
    """
    _add_restaurant(test_db)
    db.upsert_dish(1, "Pho", "small bowl", "$12", db_path=test_db)
    db.upsert_dish(1, "Pho", "large bowl", "$16", db_path=test_db)
    assert len(_dish_rows(test_db)) == 2

    db.upsert_dish(1, "Combo", None, "$10", db_path=test_db)
    db.upsert_dish(1, "Combo", None, "$14", db_path=test_db)
    assert len([r for r in _dish_rows(test_db) if r["name"] == "Combo"]) == 2


def test_reprice_works_without_a_description(test_db):
    """48% of stored dishes have no description; they must still reprice."""
    _add_restaurant(test_db)
    first = db.upsert_dish(1, "Edamame", None, "$5.99", db_path=test_db)
    second = db.upsert_dish(
        1, "Edamame", None, "$6.99", db_path=test_db, allow_reprice=True
    )
    assert first == second
    assert [r["price"] for r in _dish_rows(test_db)] == ["$6.99"]


def test_reprice_never_guesses_between_indistinguishable_variants(test_db):
    """With no description to tell same-name variants apart, insert.

    Picking one to reprice could silently rewrite a real size variant, so an
    ambiguous reprice stays a new row for the dedupe pass to consider.
    """
    _add_restaurant(test_db)
    db.upsert_dish(1, "Combo", None, "$10", db_path=test_db)
    db.upsert_dish(1, "Combo", None, "$14", db_path=test_db)
    db.upsert_dish(1, "Combo", None, "$15", db_path=test_db, allow_reprice=True)
    assert len(_dish_rows(test_db)) == 3

    # A description breaks the tie, so that variant updates in place.
    db.upsert_dish(1, "Pho", "small bowl", "$12", db_path=test_db)
    db.upsert_dish(1, "Pho", "large bowl", "$16", db_path=test_db)
    db.upsert_dish(
        1, "Pho", "large bowl", "$17", db_path=test_db, allow_reprice=True
    )
    pho = [r for r in _dish_rows(test_db) if r["name"] == "Pho"]
    assert sorted(r["price"] for r in pho) == ["$12", "$17"]


def test_delta_can_remove_and_reprice_in_one_pass(test_db, monkeypatch):
    """Removals and repricings in the same delta must not interfere."""
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 9)]
    _run_with(monkeypatch, test_db, [_result(seed)])
    assert len(_dish_rows(test_db)) == 8

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [
            _result(
                [_dish("Dish 5", "$50.00"), _dish("Dish 6", "$60.00")],
                removed=["Dish 1", "Dish 2"],
                mode="delta",
            )
        ],
        mode="auto",
    )
    rows = {r["name"]: r["price"] for r in _dish_rows(test_db)}
    assert len(rows) == 6, f"expected 8 - 2 removed, got {rows}"
    assert "Dish 1" not in rows and "Dish 2" not in rows
    assert rows["Dish 5"] == "$50.00"
    assert rows["Dish 6"] == "$60.00"


def test_delta_re_adding_a_removed_dish_is_an_insert(test_db, monkeypatch):
    """A dish deleted earlier and re-emitted later comes back as a new row.

    Not a reprice: there is nothing left to reprice, so the count grows.
    """
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 9)]
    _run_with(monkeypatch, test_db, [_result(seed)])

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([], removed=["Dish 3"], mode="delta")],
        mode="auto",
    )
    assert len(_dish_rows(test_db)) == 7

    _set_menu(test_db, 1, "v3")
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("Dish 3", "$99.00")], mode="delta")],
        mode="auto",
    )
    rows = {r["name"]: r["price"] for r in _dish_rows(test_db)}
    assert len(rows) == 8
    assert rows["Dish 3"] == "$99.00"


def test_repeated_rescrapes_do_not_grow_the_menu(test_db, monkeypatch):
    """Ten recrawls with drifting formatting must stay at N rows, not 10N."""
    _add_restaurant(test_db)
    variants = [
        ("Falafel Wrap", "$9.00", "tahini"),
        ("FALAFEL WRAP", "$9", "tahini"),
        ("falafel wrap", "$9.0", "tahini"),
        ("Falafel  Wrap", "$9.00", None),
        ("Falafel-Wrap", "$9", "tahini"),
    ]
    for i, v in enumerate(variants * 2):
        _set_menu(test_db, 1, f"v{i}")
        _run_with(monkeypatch, test_db, [_result([_dish(*v)])])
    rows = _dish_rows(test_db)
    assert len(rows) == 1, f"formatting drift accumulated rows: {rows}"


def test_delta_mode_does_not_duplicate_on_reformatted_name(test_db, monkeypatch):
    """Delta re-emits a dish with drifted formatting -> update, not insert."""
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 7)]
    _run_with(monkeypatch, test_db, [_result(seed)])
    assert len(_dish_rows(test_db)) == 6

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([_dish("DISH 3", "$3")], mode="delta")],
        mode="auto",
    )
    assert len(_dish_rows(test_db)) == 6, "delta re-emit duplicated a dish"


# --------------------------------------------------------------------------
# 2b. The uniqueness migration on a legacy DB
# --------------------------------------------------------------------------

def test_migration_widens_uniqueness_preserving_rows_and_ids(tmp_path):
    """Rebuilding dishes must keep every row, id and FK link intact."""
    path = str(tmp_path / "legacy.db")
    db.init_db(path)
    # Recreate the OLD narrow constraint to simulate a pre-migration DB.
    with db.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            DROP TABLE IF EXISTS dishes_legacy;
            CREATE TABLE dishes_legacy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
                name TEXT NOT NULL, raw_description TEXT, price TEXT,
                calories TEXT, category TEXT,
                UNIQUE (restaurant_id, name)
            );
            INSERT INTO dishes_legacy SELECT id, restaurant_id, name,
                raw_description, price, calories, category FROM dishes;
            DROP TABLE dishes;
            ALTER TABLE dishes_legacy RENAME TO dishes;
            PRAGMA foreign_keys = ON;
            """
        )
        conn.execute(
            "INSERT INTO restaurants (id,name,address,place_id) "
            "VALUES (1,'R','a','p1')"
        )
        conn.execute(
            "INSERT INTO dishes (id,restaurant_id,name,price) "
            "VALUES (42,1,'Pho','$12')"
        )
        conn.execute(
            """
            INSERT INTO classifications
                (dish_id, verdict, confidence, reasoning, model_version, created_at)
            VALUES (42,'vegan',0.9,'r','m','2026-01-01T00:00:00+00:00')
            """
        )

    db.init_db(path)  # applies the migration

    with db.connect(path) as conn:
        schema = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='dishes'"
            ).fetchone()[0].split()
        ).lower()
        assert "unique (restaurant_id, name, price)" in schema
        assert conn.execute("SELECT COUNT(*) c FROM dishes").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT id FROM dishes"
        ).fetchone()["id"] == 42, "row ids must survive the rebuild"
        assert conn.execute(
            "SELECT COUNT(*) c FROM classifications WHERE dish_id = 42"
        ).fetchone()["c"] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # Idempotent: a second init_db is a no-op, and no temp table lingers.
    db.init_db(path)
    with db.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM dishes").fetchone()["c"] == 1
        leftovers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'dishes_%'"
        ).fetchall()
        assert leftovers == []

    # And the whole point: variants no longer collapse.
    assert db.upsert_dish(1, "Pho", None, "$12", db_path=path) == 42
    assert db.upsert_dish(1, "Pho", None, "$16", db_path=path) != 42


# --------------------------------------------------------------------------
# 3. Removals
# --------------------------------------------------------------------------

def test_delta_removes_named_dishes(test_db, monkeypatch):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 7)]
    _run_with(monkeypatch, test_db, [_result(seed)])

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([], removed=["Dish 2"], mode="delta")],
        mode="auto",
    )
    assert "Dish 2" not in _names(test_db)
    assert len(_dish_rows(test_db)) == 5


def test_delta_mass_removal_is_distrusted_and_falls_back_to_full(test_db, monkeypatch):
    """A delta erasing most of the menu is a misread; full re-extract wins."""
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 11)]
    _run_with(monkeypatch, test_db, [_result(seed)])
    assert len(_dish_rows(test_db)) == 10

    _set_menu(test_db, 1, "v2")
    bogus_delta = _result([], removed=[f"Dish {i}" for i in range(1, 10)], mode="delta")
    full_rescue = _result([_dish(f"Dish {i}", f"${i}.00") for i in range(1, 11)])
    _run_with(monkeypatch, test_db, [bogus_delta, full_rescue], mode="auto")

    assert len(_dish_rows(test_db)) == 10, "distrust guard failed; menu was erased"


def test_removed_dish_takes_its_classifications_with_it(test_db, monkeypatch):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 7)]
    _run_with(monkeypatch, test_db, [_result(seed)])

    with db.connect(test_db) as conn:
        dish_id = conn.execute(
            "SELECT id FROM dishes WHERE restaurant_id = 1 AND name = 'Dish 2'"
        ).fetchone()["id"]
        assert conn.execute(
            "SELECT COUNT(*) c FROM classifications WHERE dish_id = ?", (dish_id,)
        ).fetchone()["c"] > 0

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([], removed=["Dish 2"], mode="delta")],
        mode="auto",
    )
    with db.connect(test_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM classifications WHERE dish_id = ?", (dish_id,)
        ).fetchone()["c"] == 0


def test_full_mode_drops_departed_dishes_and_their_classifications(test_db, monkeypatch):
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    _run_with(
        monkeypatch, test_db, [_result([_dish("Hummus", "$6"), _dish("Falafel", "$8")])]
    )
    _set_menu(test_db, 1, "v2")
    _run_with(monkeypatch, test_db, [_result([_dish("Hummus", "$6")])])

    assert _names(test_db) == ["Hummus"]
    with db.connect(test_db) as conn:
        orphans = conn.execute(
            """
            SELECT COUNT(*) c FROM classifications
            WHERE dish_id NOT IN (SELECT id FROM dishes)
            """
        ).fetchone()["c"]
    assert orphans == 0


def test_delta_removal_tolerates_name_formatting_drift(test_db, monkeypatch):
    """The model echoes removed names from its own read of the menu, so the
    casing/punctuation may not match the stored row. That must still delete."""
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 7)]
    _run_with(monkeypatch, test_db, [_result(seed)])

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([], removed=["DISH 2"], mode="delta")],
        mode="auto",
    )
    remaining = _names(test_db)
    assert "Dish 2" not in remaining
    assert len(remaining) == 5


def test_delta_removal_is_ambiguous_across_price_variants(test_db, monkeypatch):
    """A canonical name matching several price variants deletes nothing.

    Guessing which variant left the menu risks deleting the wrong one, so an
    ambiguous removal is a deliberate no-op.
    """
    _add_restaurant(test_db)
    _set_menu(test_db, 1, "v1")
    seed = [_dish(f"Dish {i}", f"${i}.00") for i in range(1, 6)] + [
        _dish("Pho", "$12", "small"),
        _dish("Pho", "$16", "large"),
    ]
    _run_with(monkeypatch, test_db, [_result(seed)])
    assert len(_dish_rows(test_db)) == 7

    _set_menu(test_db, 1, "v2")
    _run_with(
        monkeypatch,
        test_db,
        [_result([], removed=["PHO"], mode="delta")],
        mode="auto",
    )
    assert len(_dish_rows(test_db)) == 7

    # An exact name+price-variant match is unambiguous and still deletes.
    _set_menu(test_db, 1, "v3")
    _run_with(
        monkeypatch,
        test_db,
        [_result([], removed=["Dish 3"], mode="delta")],
        mode="auto",
    )
    assert len(_dish_rows(test_db)) == 6
