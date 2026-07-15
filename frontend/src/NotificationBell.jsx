import { useContext, useEffect, useRef, useState } from "react";
import {
  CLOUD_ENABLED,
  SessionContext,
  fetchMyNotes,
  fetchNotificationEvents,
  fetchNotificationsSeenAt,
  markNotificationsSeen,
  restaurantNameForPlaceId,
} from "./cloud.js";
import {
  groupNotesByPlace,
  isUnreadNotification,
  unreadNotificationCount,
} from "./notifications.js";
import { replyPreview } from "./commentReplies.js";
import { DEFAULT_PUBLIC_NAME } from "./username.js";

function timeAgo(iso) {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString([], {
    month: "short",
    day: "numeric",
  });
}

function placeLabel(placeId) {
  return restaurantNameForPlaceId(placeId) || "a restaurant";
}

// Reuse the magic-link return path: a full navigation with ?comments=<place>
// reopens that restaurant on its notes tab once the datasets load.
function openRestaurantNotes(placeId) {
  const url = new URL(window.location.href);
  url.searchParams.set("comments", placeId);
  url.searchParams.delete("note");
  url.hash = "#restaurants";
  window.location.assign(url.toString());
}

// Header bell for signed-in users: replies to your notes and @mentions of
// you, plus a second tab listing every note you have written. Unread state
// is the account-level seen_at watermark, so the badge clears on every
// device once the panel is opened anywhere.
export default function NotificationBell() {
  const session = useContext(SessionContext);
  const userId = session?.user?.id;
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("activity");
  const [events, setEvents] = useState(null);
  const [seenAt, setSeenAt] = useState(null);
  // Freeze the highlight watermark while the panel is open: marking the
  // panel seen must not instantly un-highlight the rows being read.
  const [highlightSince, setHighlightSince] = useState(null);
  const [myNotes, setMyNotes] = useState(null);
  const [error, setError] = useState(null);
  const popRef = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!CLOUD_ENABLED || !userId) {
      setEvents(null);
      setSeenAt(null);
      setMyNotes(null);
      setOpen(false);
      return undefined;
    }
    let cancelled = false;
    const load = () => {
      // Two independent fetches: the feed must render even when the seen_at
      // watermark is unavailable (and vice versa).
      fetchNotificationEvents(userId)
        .then((nextEvents) => {
          if (cancelled) return;
          setEvents(nextEvents);
          setError(null);
        })
        .catch((loadError) => {
          console.warn("Could not load notifications:", loadError.message);
          if (cancelled) return;
          setEvents((current) => current ?? []);
          setError("Could not load notifications.");
        });
      fetchNotificationsSeenAt(userId)
        .then((nextSeenAt) => {
          if (cancelled) return;
          // The watermark only ever advances: opening the panel on another
          // device clears this one, and a stale refetch never regresses it.
          setSeenAt((current) =>
            !current ||
            (nextSeenAt && new Date(nextSeenAt) > new Date(current))
              ? nextSeenAt
              : current
          );
        })
        .catch(() => {});
    };
    load();
    window.addEventListener("dishtune:comments-changed", load);
    return () => {
      cancelled = true;
      window.removeEventListener("dishtune:comments-changed", load);
    };
  }, [userId]);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event) => {
      if (popRef.current && !popRef.current.contains(event.target)) {
        setOpen(false);
      }
    };
    const closeOnEscape = (event) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  if (!CLOUD_ENABLED || !userId) return null;

  const unread = unreadNotificationCount(events || [], seenAt);

  function togglePanel() {
    const next = !open;
    setOpen(next);
    if (!next) return;
    setError(null);
    setHighlightSince(seenAt);
    // Opening counts as reading: advance the cross-device watermark.
    markNotificationsSeen(userId)
      .then((stamp) => stamp && setSeenAt(stamp))
      .catch(() => {});
    if (myNotes === null) {
      fetchMyNotes(userId)
        .then(setMyNotes)
        .catch(() => setError("Could not load your notes."));
    }
  }

  const noteGroups = groupNotesByPlace(myNotes || []);

  return (
    <div className="relative" ref={popRef}>
      <button
        ref={triggerRef}
        type="button"
        onClick={togglePanel}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={
          unread > 0
            ? `Notifications, ${unread} unread`
            : "Notifications"
        }
        title="Replies, mentions, and your notes"
        className={`relative flex h-9 w-9 items-center justify-center rounded-full border transition ${
          unread > 0
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-stone-200 bg-white text-stone-500 hover:border-emerald-300 hover:text-emerald-700"
        }`}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-4.5 w-4.5"
        >
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.7 21a2 2 0 0 1-3.4 0" />
        </svg>
        {unread > 0 && (
          <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-bold text-white">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Notifications"
          className="absolute right-0 z-40 mt-2 w-80 max-w-[calc(100vw-2rem)] rounded-xl border border-stone-200 bg-white shadow-lg"
        >
          <div className="flex gap-1 border-b border-stone-100 p-2">
            {[
              ["activity", "Activity"],
              ["notes", "Your notes"],
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`rounded-full px-3 py-1 text-xs font-bold transition ${
                  tab === key
                    ? "bg-emerald-700 text-white"
                    : "text-stone-500 hover:bg-stone-100 hover:text-stone-800"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="max-h-96 overflow-y-auto p-2">
            {tab === "activity" ? (
              !events || events.length === 0 ? (
                <p className="px-2 py-6 text-center text-xs text-stone-400">
                  {events === null
                    ? error || "Loading…"
                    : error ||
                      "No replies or mentions yet. When someone answers one of your notes or @mentions you, it shows up here."}
                </p>
              ) : (
                <ul className="space-y-1">
                  {events.map((event) => {
                    const fresh = isUnreadNotification(event, highlightSince);
                    const who = event.profiles?.username
                      ? `@${event.profiles.username}`
                      : DEFAULT_PUBLIC_NAME;
                    return (
                      <li key={event.id}>
                        <button
                          type="button"
                          onClick={() => openRestaurantNotes(event.place_id)}
                          className={`w-full rounded-lg px-2 py-2 text-left transition hover:bg-stone-50 ${
                            fresh ? "bg-emerald-50/70" : ""
                          }`}
                        >
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="text-xs font-semibold text-stone-800">
                              {who}{" "}
                              <span className="font-normal text-stone-500">
                                {event.kind === "reply"
                                  ? "replied to your note at"
                                  : "mentioned you at"}
                              </span>{" "}
                              {placeLabel(event.place_id)}
                            </span>
                            <span className="shrink-0 text-[10px] text-stone-400">
                              {timeAgo(event.created_at)}
                            </span>
                          </div>
                          <p className="mt-0.5 text-xs text-stone-500">
                            {replyPreview(event.body)}
                          </p>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )
            ) : !myNotes ? (
              <p className="px-2 py-6 text-center text-xs text-stone-400">
                {error || "Loading…"}
              </p>
            ) : noteGroups.length === 0 ? (
              <p className="px-2 py-6 text-center text-xs text-stone-400">
                You have not written any notes yet. Open a restaurant and add
                one from its Notes tab.
              </p>
            ) : (
              <ul className="space-y-2">
                {noteGroups.map((group) => (
                  <li key={group.place_id}>
                    <button
                      type="button"
                      onClick={() => openRestaurantNotes(group.place_id)}
                      className="w-full rounded-lg px-2 py-2 text-left transition hover:bg-stone-50"
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-xs font-bold text-stone-800">
                          {placeLabel(group.place_id)}
                        </span>
                        <span className="shrink-0 text-[10px] text-stone-400">
                          {group.notes.length}{" "}
                          {group.notes.length === 1 ? "note" : "notes"}
                        </span>
                      </div>
                      {group.notes.slice(0, 2).map((note) => (
                        <p key={note.id} className="mt-0.5 text-xs text-stone-500">
                          {replyPreview(note.body)}
                          <span className="ml-1 text-[10px] text-stone-400">
                            {timeAgo(note.created_at)}
                          </span>
                        </p>
                      ))}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
