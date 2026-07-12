import NoteIcon from "./NoteIcon.jsx";

export default function DishCommentBadge({
  count = 0,
  dishName,
  onClick,
  className = "",
}) {
  if (!count) return null;
  const noun = count === 1 ? "note" : "notes";
  const label = `View ${count} community ${noun} mentioning ${
    dishName || "this dish"
  }`;
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border border-sky-200 bg-sky-50 px-2 py-1 text-[11px] font-bold tabular-nums text-sky-700 transition hover:border-sky-400 hover:bg-sky-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:ring-offset-1 ${className}`}
    >
      <NoteIcon className="h-4 w-4" />
      <span>{count}</span>
      <span className="max-sm:hidden">{noun}</span>
    </button>
  );
}
