const BADGES = [
  ["dairy_status", "Dairy-free", "bg-sky-50 text-sky-800"],
  ["gluten_status", "Gluten-free", "bg-amber-50 text-amber-800"],
  ["nut_status", "Nut-free", "bg-orange-50 text-orange-800"],
];

export default function DietaryBadges({ dish, maxBadges = Infinity }) {
  const badges = [];
  // A side/accompaniment is not a meal — surface that up front so a bag of
  // chips can't masquerade as a dinner option.
  if (dish.serving_role === "meal") {
    badges.push({
      key: "serving-role",
      label: "Full meal",
      style: "bg-emerald-100 text-emerald-800",
      title: "Classified as substantial enough to be a main meal",
    });
  } else if (dish.serving_role === "side") {
    badges.push({
      key: "serving-role",
      label: "Side / small plate",
      style: "bg-stone-200 text-stone-700",
      title: "Classified as an accompaniment, snack, or small plate rather than a full meal",
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
      ].map(([label, status]) => (
        <div key={label} className="rounded-xl bg-stone-50 px-3 py-2">
          <div className="text-xs font-bold uppercase tracking-wide text-stone-400">{label}</div>
          <div className="mt-0.5 font-semibold text-stone-700">{labelFor(status)}</div>
        </div>
      ))}
    </div>
  );
}
