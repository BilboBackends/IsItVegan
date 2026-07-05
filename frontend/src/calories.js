export function calorieLabel(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  return /\b(?:cal|cals|calorie|calories|kcal)\b/i.test(text)
    ? text
    : `${text} cal`;
}
