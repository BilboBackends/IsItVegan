# Dish Attributes Reference

Everything DishTune checks for on every menu item, across the two
classification passes. All values are model-inferred from menu text only
(name, description, category, restaurant context) — never guessed: for
every attribute, `unclear` / empty is the required answer when the text
gives no evidence. A wrong "free" on an allergen is harmful; an "unclear"
never is.

- **Pass 1 — vegan classification** (`classifier.py`, runs at ingest time):
  the verdict and its supporting attributes.
- **Pass 2 — attribute enrichment** (`dish_attributes.py`, backfill +
  rerunnable): discovery, dietary, and recommendation attributes. Drinks
  are skipped in this pass.

Both passes store to the `classifications` table; arrays are JSON columns.

---

## Identity & basics (from scraping)

| Field | Values | Notes |
|---|---|---|
| `name` | text | As printed on the menu |
| `raw_description` | text | Verbatim menu description |
| `price` | text | Verbatim |
| `calories` | text | Verbatim value/range when the menu prints one |
| `category` | `food` · `drink` · `dessert` | Drinks are excluded from headline vegan counts and from pass 2 |

## Pass 1 — Vegan verdict

| Field | Values | Notes |
|---|---|---|
| `verdict` | `vegan` · `likely_vegan` · `vegan_adaptable` · `not_vegan` · `unclear` | The core classification. `vegan_adaptable` = vegan if modified |
| `confidence` | 0–1 | Headline counts require `vegan`, or `likely_vegan` ≥ 0.75 |
| `reasoning` | short text | The evidence — every verdict must be explainable |
| `dairy_status` | `contains` · `free` · `unclear` | Includes derivatives (butter, ghee, whey) |
| `gluten_status` | `contains` · `free` · `unclear` | |
| `nut_status` | `contains` · `free` · `unclear` | Tree nuts + peanuts |
| `protein_level` | `low` · `moderate` · `high` · `unclear` | How substantial the dish's protein is |
| `serving_role` | `meal` · `side` · `unclear` | Keeps a bag of chips from counting like a sandwich |
| `meal_types` | array of `breakfast` · `brunch` · `lunch` · `dinner` · `snack` | Every plausible context |
| `key_ingredients` | array of ingredient strings | From the extraction pass |
| `alcohol_status` | `alcoholic` · `non_alcoholic` · `unclear` | Sections the Drinks tab; deterministic backstop in `alcohol.py` |

## Pass 2 — Enrichment attributes

### Diet & allergens

| Field | Values | Notes |
|---|---|---|
| `vegetarian_status` | `vegetarian` · `not_vegetarian` · `unclear` | Dairy/eggs OK; no meat, fish, gelatin, or meat stock. Every vegan dish is vegetarian. Cuisine-standard hidden ingredients count (fish sauce in Thai curries) |
| `meat_sources` | array (max 4) of `beef` · `pork` · `chicken` · `turkey` · `duck` · `lamb` · `goat` · `veal` · `fish` · `shellfish` · `other_meat` | Every meat present or definitionally standard (pepperoni → pork, Caesar dressing → fish). Empty = no meat identified. Powers pescatarian / no-pork / no-red-meat filtering |
| `egg_status` | `contains` · `free` · `unclear` | Derivatives count (mayo, aioli, most waffles/pancakes) |
| `soy_status` | `contains` · `free` · `unclear` | Tofu, soy sauce, miso, edamame |
| `sesame_status` | `contains` · `free` · `unclear` | Tahini, sesame oil/seeds |
| `vegan_adaptation` | text or null | Only for `vegan_adaptable` dishes: one imperative sentence for the diner ("Ask for no cheese.") |

Together with pass 1, allergen coverage spans dairy, gluten, nuts, egg,
soy, sesame, fish, and shellfish — near-complete big-9 coverage.

### Protein

| Field | Values | Notes |
|---|---|---|
| `protein_source` | `meat_analogue` · `tofu_tempeh_seitan` · `legume` · `nut` · `animal` · `none` · `unclear` | The PRIMARY protein. `meat_analogue` only for explicit substitutes (Impossible, Beyond, plant-based chick'n) — lets users seek out or avoid fake meat |

### Character & discovery (recommendation features)

| Field | Values | Notes |
|---|---|---|
| `spice_level` | `none` · `mild` · `medium` · `hot` · `unclear` | Explicit evidence only (chili markers, "spicy", vindaloo) |
| `cooking_method` | `fried` · `grilled` · `baked` · `raw` · `steamed` · `boiled_simmered` · `sauteed` · `mixed` · `unclear` | Dominant stated/definitionally certain method. `raw` only when raw is the point (salad, poke, crudo) |
| `dish_format` | `bowl` · `poke` · `sushi` · `ramen` · `pho` · `noodle_dish` · `pasta` · `pizza` · `flatbread` · `sandwich` · `wrap` · `burrito` · `taco` · `quesadilla` · `nachos` · `burger` · `hot_dog` · `gyro` · `kebab` · `dumpling` · `empanada` · `spring_roll` · `curry` · `stir_fry` · `fried_rice` · `rice_dish` · `soup` · `stew` · `chili` · `salad` · `small_plate` · `plate` · `breakfast_plate` · `omelet` · `pancake_waffle` · `baked_good` · `pastry` · `dessert` · `drink` · `other` | What the dish is like to eat. Most specific value wins (`pho` over `soup`). Widen the enum later and re-run only the `other` rows |
| `flavor_profile` | array (max 3) of `creamy_rich` · `fresh_light` · `tangy` · `sweet` · `smoky` · `savory` · `herbal` · `garlicky` · `umami` | Only what the description supports; empty = no flavor evidence |
| `ingredient_tags` | array (max 6) of canonical lowercase singular nouns | "chickpea" not "crispy chickpeas" — normalized so preference learning can match likes across restaurants |

### Bookkeeping

| Field | Notes |
|---|---|
| `attributes_enriched_at` | UTC timestamp; NULL = not yet enriched (the backfill's resume marker) |
| `attributes_model` | Which model produced the pass-2 attributes |

---

## Not captured on purpose

- **Per-dish culture/cuisine** — restaurant-level cuisine answers the need;
  per-dish guesses from names are the biggest hallucination risk.
- **Healthiness scores, kid-friendliness, portion size** — no evidence in
  menu text; the model would be inventing.
- **Shared-fryer / cross-contamination** — unknowable from a menu; never
  implied.
