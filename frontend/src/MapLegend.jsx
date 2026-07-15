import { useId, useState } from "react";

export default function MapLegend({ children, isDesktop }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const showContent = isDesktop || expanded;

  return (
    <aside
      aria-label="Map legend"
      onPointerDown={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
      style={{
        bottom: isDesktop
          ? "1rem"
          : "calc(1rem + env(safe-area-inset-bottom, 0px))",
      }}
      className={`absolute left-4 z-[500] overflow-visible ${
        isDesktop ? "pointer-events-none" : "pointer-events-auto"
      }`}
    >
      {!isDesktop && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={contentId}
          aria-label={expanded ? "Collapse map legend" : "Expand map legend"}
          onClick={(event) => {
            event.stopPropagation();
            setExpanded((current) => !current);
          }}
          className="pointer-events-auto flex min-h-11 w-full items-center justify-between gap-3 rounded-xl border border-stone-200 bg-white/95 px-3 py-2 text-xs font-bold text-stone-700 shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-600"
        >
          <span className="inline-flex items-center gap-1.5">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="h-3.5 w-3.5 text-emerald-700"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="9" />
              <path d="M12 10v6" />
              <path d="M12 7h.01" />
            </svg>
            Legend
          </span>
          <span className="text-base leading-none text-stone-400" aria-hidden="true">
            {expanded ? "−" : "+"}
          </span>
        </button>
      )}

      {showContent && (
        <div
          id={contentId}
          className={
            isDesktop
              ? "w-[13.5rem] overflow-hidden rounded-xl border border-stone-200 bg-white/95 px-3 py-2 shadow-md"
              : "absolute bottom-full left-0 mb-2 w-[13.5rem] overflow-hidden rounded-xl border border-stone-200 bg-white/95 px-3 py-2 shadow-md"
          }
        >
          {children}
        </div>
      )}
    </aside>
  );
}
