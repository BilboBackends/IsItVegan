export default function FilterSidebar({ open, onToggle, activeCount = 0, children }) {
  return (
    <aside
      className={`w-full shrink-0 transition-[width] duration-200 ${
        open ? "lg:w-64" : "lg:w-14"
      }`}
    >
      <div className="rounded-2xl border border-stone-200 bg-white p-2 shadow-sm lg:sticky lg:top-32">
        <button
          type="button"
          onClick={onToggle}
          className={`flex w-full items-center rounded-xl px-2.5 py-2 text-sm font-bold text-stone-700 hover:bg-stone-50 ${
            open ? "justify-between" : "justify-center"
          }`}
          aria-expanded={open}
          title={open ? "Collapse filters" : "Open filters"}
        >
          <span className="flex items-center gap-2">
            <span aria-hidden="true">☷</span>
            <span className={open ? "" : "lg:sr-only"}>
              Filters{activeCount > 0 && ` · ${activeCount}`}
            </span>
          </span>
          {open && <span className="text-stone-400" aria-hidden="true">‹</span>}
        </button>
        {open && (
          <div className="mt-2 max-h-[calc(100vh-10rem)] space-y-3 overflow-y-auto px-1 pb-1">
            {children}
          </div>
        )}
      </div>
    </aside>
  );
}
