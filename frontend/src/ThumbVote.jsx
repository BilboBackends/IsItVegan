import { useContext, useState } from "react";
import { STATIC_MODE } from "./staticData.js";
import { SessionContext, syncVote } from "./cloud.js";

// Thumbs up/down on a dish OR a restaurant — the lightweight "was this
// right / did you like it" signal, with a running count next to each thumb.
// Votes live in the browser like hearts do (works on the public static
// site); when the local backend is around, each vote is also recorded.
//
// One vote per browser per target: an anonymous client id accompanies every
// vote, so re-clicking switches or withdraws THIS browser's vote instead of
// stacking new rows — the count can't be inflated by mashing the button.

const CLIENT_KEY = "veganfind:clientId";

const KINDS = {
  dish: {
    storageKey: "veganfind:dishVotes",
    endpoint: "/api/dish-votes",
    idField: "dish_id",
  },
  restaurant: {
    storageKey: "veganfind:restaurantVotes",
    endpoint: "/api/restaurant-votes",
    idField: "restaurant_id",
  },
};

function readVotes(storageKey) {
  try {
    return JSON.parse(localStorage.getItem(storageKey)) || {};
  } catch {
    return {};
  }
}

function writeVotes(storageKey, votes) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(votes));
  } catch {
    /* storage full/blocked — the buttons still toggle for the session */
  }
}

function clientId() {
  try {
    let id = localStorage.getItem(CLIENT_KEY);
    if (!id) {
      id = crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      localStorage.setItem(CLIENT_KEY, id);
    }
    return id;
  } catch {
    return null; // blocked storage: vote still counts, just not deduped
  }
}

export default function ThumbVote({
  dishId,
  restaurantId,
  upVotes = 0,
  downVotes = 0,
}) {
  const kind = restaurantId != null ? KINDS.restaurant : KINDS.dish;
  const targetId = restaurantId ?? dishId;
  const session = useContext(SessionContext);
  const [vote, setVote] = useState(
    () => readVotes(kind.storageKey)[targetId] || null
  );
  // The server counts already include this browser's stored vote (when the
  // backend recorded it); remember what we started with so toggling adjusts
  // the displayed number without double-counting ourselves.
  const [initialVote] = useState(vote);

  function cast(next, event) {
    event.stopPropagation();
    const value = vote === next ? null : next; // tap again to clear
    setVote(value);
    const all = readVotes(kind.storageKey);
    if (value) all[targetId] = value;
    else delete all[targetId];
    writeVotes(kind.storageKey, all);
    // Signed-in likes persist to the account (deduped per user by the DB).
    if (session?.user) {
      syncVote(
        restaurantId != null ? "restaurants" : "dishes",
        targetId,
        value,
        session.user.id
      ).catch(() => {});
    }
    if (!STATIC_MODE) {
      fetch(kind.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          [kind.idField]: targetId,
          vote: value,
          client_id: clientId(),
        }),
      }).catch(() => {});
    }
  }

  const shown = (base, side) =>
    Math.max(
      0,
      (base || 0) + (vote === side ? 1 : 0) - (initialVote === side ? 1 : 0)
    );
  const upCount = shown(upVotes, "up");
  const downCount = shown(downVotes, "down");

  const base =
    "flex items-center gap-1 rounded-full px-1.5 py-0.5 text-sm leading-none transition select-none";
  return (
    <span className="flex shrink-0 items-center gap-0.5" title="Rate this item">
      <button
        onClick={(e) => cast("up", e)}
        aria-label={`Thumbs up (${upCount})`}
        aria-pressed={vote === "up"}
        className={`${base} ${
          vote === "up"
            ? "bg-emerald-100"
            : "opacity-40 grayscale hover:opacity-80 hover:grayscale-0"
        }`}
      >
        👍
        {upCount > 0 && (
          <span className="text-xs font-bold text-emerald-800">{upCount}</span>
        )}
      </button>
      <button
        onClick={(e) => cast("down", e)}
        aria-label={`Thumbs down (${downCount})`}
        aria-pressed={vote === "down"}
        className={`${base} ${
          vote === "down"
            ? "bg-rose-100"
            : "opacity-40 grayscale hover:opacity-80 hover:grayscale-0"
        }`}
      >
        👎
        {downCount > 0 && (
          <span className="text-xs font-bold text-rose-700">{downCount}</span>
        )}
      </button>
    </span>
  );
}
