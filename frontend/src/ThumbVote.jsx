import { useState } from "react";
import { STATIC_MODE } from "./staticData.js";

// Thumbs up/down on a dish — the lightweight "was this right / did you like
// it" signal. Votes live in the browser like hearts do (works on the public
// static site); when the local backend is around, each vote is also recorded
// in dish_votes so the pipeline owner accumulates feedback data.

const STORAGE_KEY = "veganfind:dishVotes";

function readVotes() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch {
    return {};
  }
}

function writeVotes(votes) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(votes));
  } catch {
    /* storage full/blocked — the buttons still toggle for the session */
  }
}

export default function ThumbVote({ dishId }) {
  const [vote, setVote] = useState(() => readVotes()[dishId] || null);

  function cast(next, event) {
    event.stopPropagation();
    const value = vote === next ? null : next; // tap again to clear
    setVote(value);
    const all = readVotes();
    if (value) all[dishId] = value;
    else delete all[dishId];
    writeVotes(all);
    if (!STATIC_MODE && value) {
      fetch("/api/dish-votes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dish_id: dishId, vote: value }),
      }).catch(() => {});
    }
  }

  const base =
    "rounded-full px-1.5 py-0.5 text-sm leading-none transition select-none";
  return (
    <span className="flex shrink-0 items-center gap-0.5" title="Rate this item">
      <button
        onClick={(e) => cast("up", e)}
        aria-label="Thumbs up"
        aria-pressed={vote === "up"}
        className={`${base} ${
          vote === "up"
            ? "bg-emerald-100"
            : "opacity-40 grayscale hover:opacity-80 hover:grayscale-0"
        }`}
      >
        👍
      </button>
      <button
        onClick={(e) => cast("down", e)}
        aria-label="Thumbs down"
        aria-pressed={vote === "down"}
        className={`${base} ${
          vote === "down"
            ? "bg-rose-100"
            : "opacity-40 grayscale hover:opacity-80 hover:grayscale-0"
        }`}
      >
        👎
      </button>
    </span>
  );
}
