import { useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  CLOUD_ENABLED,
  SessionContext,
  deleteComment,
  dishKey,
  fetchComments,
  postComment,
  reportComment,
} from "./cloud.js";

// Per-restaurant discussion thread. Typing "@" in the composer suggests the
// restaurant's dishes; accepted mentions are stored as structured
// {dish_key, dish_name} pairs so tips deep-link to dishes and survive the
// pipeline renumbering dish ids. Signed-in users only can post (RLS enforces
// it server-side; this UI just mirrors that).
export default function Comments({ restaurant, dishes, onOpenDish }) {
  const session = useContext(SessionContext);
  const [comments, setComments] = useState(null);
  const [body, setBody] = useState("");
  const [mentions, setMentions] = useState([]); // [{dish_key, dish_name}]
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const textareaRef = useRef(null);

  const placeId = restaurant?.place_id;

  useEffect(() => {
    if (!CLOUD_ENABLED || !placeId) return;
    let cancelled = false;
    fetchComments(placeId)
      .then((rows) => !cancelled && setComments(rows))
      .catch(() => !cancelled && setComments([]));
    return () => {
      cancelled = true;
    };
  }, [placeId]);

  // "@" suggestions: the token being typed after the last unaccepted "@".
  const mentionQuery = useMemo(() => {
    const match = /@([^@\n]{0,40})$/.exec(body);
    return match ? match[1].toLowerCase() : null;
  }, [body]);

  const suggestions = useMemo(() => {
    if (mentionQuery == null) return [];
    const names = [...new Set((dishes || []).map((d) => d.name))];
    return names
      .filter((name) => name.toLowerCase().includes(mentionQuery))
      .slice(0, 6);
  }, [mentionQuery, dishes]);

  function acceptMention(name) {
    setBody((current) => current.replace(/@[^@\n]{0,40}$/, `@${name} `));
    setMentions((current) =>
      current.some((m) => m.dish_key === dishKey(name))
        ? current
        : [...current, { dish_key: dishKey(name), dish_name: name }]
    );
    textareaRef.current?.focus();
  }

  async function submit(event) {
    event.preventDefault();
    const text = body.trim();
    if (!text || busy || !session?.user) return;
    setBusy(true);
    setError(null);
    try {
      // Keep only mentions still present in the final text.
      const kept = mentions.filter((m) => body.includes(`@${m.dish_name}`));
      const row = await postComment(placeId, text, kept, session.user.id);
      setComments((current) => [row, ...(current || [])]);
      setBody("");
      setMentions([]);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    try {
      await deleteComment(id);
      setComments((current) => current.filter((c) => c.id !== id));
    } catch (e) {
      setError(e.message);
    }
  }

  async function report(id) {
    try {
      await reportComment(id, session.user.id);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }

  // Render @Dish Name tokens as chips that open the dish.
  function renderBody(comment) {
    const names = (comment.mentions || [])
      .map((m) => m.dish_name)
      .filter(Boolean)
      .sort((a, b) => b.length - a.length);
    if (names.length === 0) return comment.body;
    const pattern = new RegExp(
      `@(${names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`,
      "g"
    );
    const parts = comment.body.split(pattern);
    return parts.map((part, i) => {
      if (!names.includes(part)) return part;
      const dish = (dishes || []).find((d) => d.name === part);
      return (
        <button
          key={`${part}-${i}`}
          onClick={() => dish && onOpenDish?.(dish)}
          className="mx-0.5 inline-flex items-center rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-800 hover:bg-emerald-100"
        >
          🍽 {part}
        </button>
      );
    });
  }

  if (!CLOUD_ENABLED || !placeId) return null;

  return (
    <section className="border-t border-stone-200 px-4 py-4 sm:px-6">
      <h3 className="text-sm font-bold text-stone-800">
        Tips & comments
        {comments?.length > 0 && (
          <span className="ml-1.5 text-xs font-medium text-stone-400">
            {comments.length}
          </span>
        )}
      </h3>

      {session?.user ? (
        <form onSubmit={submit} className="relative mt-2">
          <textarea
            ref={textareaRef}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={2}
            maxLength={1000}
            placeholder='Share a tip — type "@" to point at a dish (e.g. "@Vegan Tacos ask for no crema")'
            className="w-full rounded-xl border border-stone-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
          />
          {suggestions.length > 0 && (
            <div className="absolute left-0 top-full z-30 mt-1 w-72 overflow-hidden rounded-xl border border-stone-200 bg-white shadow-lg">
              {suggestions.map((name) => (
                <button
                  type="button"
                  key={name}
                  onClick={() => acceptMention(name)}
                  className="block w-full truncate px-3 py-2 text-left text-sm text-stone-700 hover:bg-emerald-50"
                >
                  @{name}
                </button>
              ))}
            </div>
          )}
          <div className="mt-1.5 flex items-center justify-between">
            <span className="text-[11px] text-stone-400">
              Be kind; dish tips help everyone.
            </span>
            <button
              type="submit"
              disabled={busy || !body.trim()}
              className="rounded-lg bg-emerald-700 px-4 py-1.5 text-xs font-bold text-white hover:bg-emerald-800 disabled:bg-stone-300"
            >
              {busy ? "Posting…" : "Post"}
            </button>
          </div>
        </form>
      ) : (
        <p className="mt-2 rounded-lg bg-stone-50 px-3 py-2 text-xs text-stone-500">
          Sign in (top right) to share tips about this restaurant.
        </p>
      )}
      {error && <div className="mt-2 text-xs text-rose-600">{error}</div>}

      <div className="mt-3 space-y-3">
        {comments === null && (
          <div className="text-xs text-stone-400">Loading comments…</div>
        )}
        {comments?.length === 0 && (
          <div className="text-xs text-stone-400">
            No tips yet — be the first.
          </div>
        )}
        {(comments || []).map((comment) => (
          <div key={comment.id} className="rounded-xl bg-stone-50 px-3 py-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs font-bold text-stone-700">
                {comment.profiles?.display_name || "vegan explorer"}
              </span>
              <span className="shrink-0 text-[10px] text-stone-400">
                {new Date(comment.created_at).toLocaleDateString([], {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })}
              </span>
            </div>
            <div className="mt-1 whitespace-pre-wrap text-sm text-stone-700">
              {renderBody(comment)}
            </div>
            {session?.user && (
              <div className="mt-1 flex gap-3 text-[10px] font-semibold">
                {comment.user_id === session.user.id ? (
                  <button
                    onClick={() => remove(comment.id)}
                    className="text-stone-400 hover:text-rose-600"
                  >
                    Delete
                  </button>
                ) : (
                  <button
                    onClick={() => report(comment.id)}
                    className="text-stone-300 hover:text-amber-600"
                    title="Flag as spam or abuse"
                  >
                    Report
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
