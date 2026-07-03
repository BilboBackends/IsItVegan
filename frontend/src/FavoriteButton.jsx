export default function FavoriteButton({ active, onClick, label = "item" }) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-lg transition ${
        active
          ? "border-rose-200 bg-rose-50 text-rose-600"
          : "border-stone-200 bg-white text-stone-400 hover:border-rose-200 hover:text-rose-500"
      }`}
      title={active ? `Remove ${label} from Saved` : `Save ${label}`}
      aria-label={active ? `Remove ${label} from Saved` : `Save ${label}`}
      aria-pressed={active}
    >
      {active ? "♥" : "♡"}
    </button>
  );
}
