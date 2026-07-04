const BADGES = [
  ["dairy_status", "Dairy-free", "bg-sky-50 text-sky-800"],
  ["gluten_status", "Gluten-free", "bg-amber-50 text-amber-800"],
  ["nut_status", "Nut-free", "bg-orange-50 text-orange-800"],
];

export default function DietaryBadges({ dish, includeMeals = false }) {
  const badges = BADGES.filter(([field]) => dish[field] === "free").map(
    ([field, label, style]) => ({ key: field, label, style })
  );
  if (dish.protein_level === "high") {
    badges.push({
      key: "protein",
      label: "High protein",
      style: "bg-violet-50 text-violet-800",
    });
  }
  if (includeMeals) {
    for (const meal of dish.meal_types || []) {
      badges.push({
        key: `meal-${meal}`,
        label: meal,
        style: "bg-stone-100 text-stone-600 capitalize",
      });
    }
  }
  if (badges.length === 0) return null;
  return (
    <>
      {badges.map((badge) => (
        <span
          key={badge.key}
          className={`rounded-full px-2.5 py-1 text-xs font-medium ${badge.style}`}
          title="Inferred from menu ingredients; confirm allergy and cross-contact needs with the restaurant"
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
      ].map(([label, status]) => (
        <div key={label} className="rounded-xl bg-stone-50 px-3 py-2">
          <div className="text-xs font-bold uppercase tracking-wide text-stone-400">{label}</div>
          <div className="mt-0.5 font-semibold text-stone-700">{labelFor(status)}</div>
        </div>
      ))}
    </div>
  );
}
