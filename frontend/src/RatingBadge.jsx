export function formatRatingCount(count) {
  if (count == null) return null;
  return new Intl.NumberFormat("en-US", {
    notation: count >= 1000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(count);
}

export function ratingText(rating, userRatingCount) {
  if (rating == null) return null;
  const count = formatRatingCount(userRatingCount);
  return `★ ${Number(rating).toFixed(1)}${count ? ` (${count})` : ""}`;
}

export default function RatingBadge({
  rating,
  userRatingCount,
  className = "",
}) {
  const text = ratingText(rating, userRatingCount);
  if (!text) return null;

  const exactCount =
    userRatingCount == null
      ? ""
      : ` from ${Number(userRatingCount).toLocaleString()} rating${
          Number(userRatingCount) === 1 ? "" : "s"
        }`;

  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold text-stone-600 ${className}`}
      title={`Google rating: ${Number(rating).toFixed(1)} out of 5${exactCount}`}
      aria-label={`Google rating ${Number(rating).toFixed(1)} out of 5${exactCount}`}
    >
      <span className="text-amber-500" aria-hidden="true">★</span>
      <span>{Number(rating).toFixed(1)}</span>
      {userRatingCount != null && (
        <span className="font-normal text-stone-400">
          ({formatRatingCount(userRatingCount)})
        </span>
      )}
      <span className="font-normal text-stone-400">Google</span>
    </span>
  );
}
