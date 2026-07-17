const BADGES = [
  ["dairy_status", "Dairy-free", "bg-sky-50 text-sky-800"],
  ["gluten_status", "Gluten-free", "bg-amber-50 text-amber-800"],
  ["nut_status", "Nut-free", "bg-orange-50 text-orange-800"],
  ["egg_status", "Egg-free", "bg-yellow-50 text-yellow-800"],
  ["soy_status", "Soy-free", "bg-lime-50 text-lime-800"],
  ["sesame_status", "Sesame-free", "bg-teal-50 text-teal-800"],
];

const SPICE_LABELS = { mild: "Mild 🌶", medium: "Spicy 🌶🌶", hot: "Hot 🌶🌶🌶" };

const DAYPARTS = ["breakfast", "brunch", "lunch", "dinner"];

export default function DietaryBadges({ dish, maxBadges = Infinity }) {
  const badges = [];
  // A side/accompaniment is not a meal — surface that up front so a bag of
  // chips can't masquerade as a dinner option. (A full meal is the normal
  // case and doesn't need a badge.)
  if (dish.serving_role === "side") {
    badges.push({
      key: "serving-role",
      label: "Side / small plate",
      style: "bg-stone-200 text-stone-700",
      title: "Classified as an accompaniment, snack, or small plate rather than a full meal",
    });
  }
  // Time-of-day availability: a dish tied to exactly one daypart (it only
  // appears on the dinner menu page) isn't available all day — say so.
  if (dish.meal_types?.length === 1 && DAYPARTS.includes(dish.meal_types[0])) {
    const meal = dish.meal_types[0];
    badges.push({
      key: "daypart",
      label: `${meal.charAt(0).toUpperCase()}${meal.slice(1)} only`,
      style: "bg-indigo-50 text-indigo-800",
      title: `Found only on the ${meal} menu — likely not served at other times of day`,
    });
  }
  if (dish.protein_level === "high") {
    badges.push({
      key: "protein",
      label: "High protein",
      style: "bg-violet-50 text-violet-800",
      title: "Inferred from the listed ingredients and typical serving",
    });
  }
  if (dish.protein_source === "meat_analogue") {
    badges.push({
      key: "meat-analogue",
      label: "Plant-based meat",
      style: "bg-fuchsia-50 text-fuchsia-800",
      title: "Uses an explicit meat substitute (Impossible, Beyond, plant-based chick'n, …)",
    });
  }
  if (SPICE_LABELS[dish.spice_level]) {
    badges.push({
      key: "spice",
      label: SPICE_LABELS[dish.spice_level],
      style: "bg-rose-50 text-rose-800",
      title: "Heat level stated on or implied by the menu",
    });
  }
  if (dish.cooking_method === "fried") {
    badges.push({
      key: "fried",
      label: "Fried",
      style: "bg-stone-100 text-stone-600",
      title: "Fried per the menu — shared-fryer practices vary; ask the restaurant if that matters to you",
    });
  }
  badges.push(
    ...BADGES.filter(([field]) => dish[field] === "free").map(
      ([field, label, style]) => ({
        key: field,
        label,
        style,
        title: "Inferred from menu ingredients; confirm allergy and cross-contact needs with the restaurant",
      })
    )
  );
  if (badges.length === 0) return null;
  const visibleBadges = badges.slice(0, maxBadges);
  return (
    <>
      {visibleBadges.map((badge) => (
        <span
          key={badge.key}
          className={`rounded-full px-2.5 py-1 text-xs font-medium ${badge.style}`}
          title={badge.title}
        >
          {badge.label}
        </span>
      ))}
    </>
  );
}

export function DietaryProfile({ dish }) {
  const labelFor = (status) => {
    if (status === "free") return "Appears free";
    if (status === "contains") return "Contains";
    return "Unclear";
  };
  return (
    <div className="grid gap-2 text-sm sm:grid-cols-3">
      {[
        ["Dairy", dish.dairy_status],
        ["Gluten", dish.gluten_status],
        ["Nuts", dish.nut_status],
        ["Egg", dish.egg_status],
        ["Soy", dish.soy_status],
        ["Sesame", dish.sesame_status],
      ]
        // Older rows without the enrichment pass have no egg/soy/sesame
        // value at all — hide those cells instead of a wall of "Unclear".
        .filter(([, status]) => status != null)
        .map(([label, status]) => (
        <div key={label} className="rounded-xl bg-stone-50 px-3 py-2">
          <div className="text-xs font-bold uppercase tracking-wide text-stone-400">{label}</div>
          <div className="mt-0.5 font-semibold text-stone-700">{labelFor(status)}</div>
        </div>
      ))}
    </div>
  );
}
