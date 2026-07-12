import { useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  CLOUD_ENABLED,
  GOOGLE_AUTH_ENABLED,
  SessionContext,
  deleteComment,
  dishKey,
  fetchComments,
  postComment,
  rememberCommentAuthReturn,
  reportComment,
  signInWithGoogle,
  signInWithMagicLink,
} from "./cloud.js";

// Per-restaurant discussion thread. Typing "@" in the composer suggests the
// restaurant's dishes; accepted mentions are stored as structured
// {dish_key, dish_name} pairs so notes deep-link to dishes and survive the
// pipeline renumbering dish ids. Signed-in users only can post (RLS enforces
// it server-side; this UI just mirrors that).
export default function Comments({
  restaurant,
  dishes,
  onOpenDish,
  // Controlled mode: a parent that needs the thread for its own UI (the
  // modal's tab badge) owns the state and passes both down.
  comments: controlledComments,
  onCommentsChange,
  initialMention = null,
  filterDish = null,
}) {
  const session = useContext(SessionContext);
  const controlled = controlledComments !== undefined;
  const [ownComments, setOwnComments] = useState(null);
  const comments = controlled ? controlledComments : ownComments;
  const setComments = controlled ? onCommentsChange : setOwnComments;
  const [body, setBody] = useState("");
  const [mentions, setMentions] = useState([]); // [{dish_key, dish_name}]
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [authSent, setAuthSent] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [activeDishFilter, setActiveDishFilter] = useState(filterDish);
  const textareaRef = useRef(null);

  const placeId = restaurant?.place_id;

  useEffect(() => {
    if (controlled || !CLOUD_ENABLED || !placeId) return;
    let cancelled = false;
    fetchComments(placeId)
      .then((rows) => !cancelled && setOwnComments(rows))
      .catch(() => !cancelled && setOwnComments([]));
    return () => {
      cancelled = true;
    };
  }, [placeId, controlled]);

  useEffect(() => {
    if (!session?.user) return undefined;
    const frame = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [session?.user]);

  useEffect(() => {
    setActiveDishFilter(filterDish || null);
  }, [filterDish]);

  useEffect(() => {
    const name =
      typeof initialMention === "string"
        ? initialMention
        : initialMention?.name;
    if (!name) return;
    const key = dishKey(name);
    setBody((current) =>
      current.includes(`@${name}`) ? current : `@${name} ${current}`
    );
    setMentions((current) =>
      current.some((mention) => mention.dish_key === key)
        ? current
        : [...current, { dish_key: key, dish_name: name }]
    );
    const frame = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [initialMention]);

  function returnToCommentsUrl() {
    const mentionName =
      typeof initialMention === "string"
        ? initialMention
        : initialMention?.name;
    const filteredDishName =
      typeof activeDishFilter === "string"
        ? activeDishFilter
        : activeDishFilter?.name;
    rememberCommentAuthReturn(placeId, mentionName);
    const url = new URL(window.location.href);
    url.searchParams.set("comments", placeId);
    if (!mentionName && filteredDishName) {
      url.searchParams.set("note", filteredDishName);
    } else {
      url.searchParams.delete("note");
    }
    // Supabase's browser OAuth flow uses the fragment for session tokens.
    // Keep our return state in the query string and leave the hash empty so
    // Google sign-in can establish the session before Explore reopens this
    // restaurant's Notes tab.
    url.hash = "";
    return url.toString();
  }

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

  async function sendSignInLink(event) {
    event.preventDefault();
    if (!email.trim() || authBusy) return;
    setAuthBusy(true);
    setAuthError(null);
    try {
      await signInWithMagicLink(email.trim(), returnToCommentsUrl());
      setAuthSent(true);
    } catch (e) {
      setAuthError(e.message);
    } finally {
      setAuthBusy(false);
    }
  }

  async function continueWithGoogle() {
    if (authBusy) return;
    setAuthBusy(true);
    setAuthError(null);
    try {
      await signInWithGoogle(returnToCommentsUrl());
    } catch (e) {
      setAuthError(e.message);
      setAuthBusy(false);
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

  const filterName =
    typeof activeDishFilter === "string"
      ? activeDishFilter
      : activeDishFilter?.name;
  const filterKey = filterName ? dishKey(filterName) : null;
  const visibleComments = filterKey
    ? (comments || []).filter((comment) =>
        (comment.mentions || []).some(
          (mention) => mention.dish_key === filterKey
        )
      )
    : comments || [];

  return (
    <section>
      <h3 className="text-sm font-bold text-stone-800">
        Community notes
        {comments?.length > 0 && (
          <span className="ml-1.5 text-xs font-medium text-stone-400">
            {comments.length}
          </span>
        )}
        <span className="ml-2 text-xs font-normal text-stone-400">
          comments · reviews · chat
        </span>
      </h3>

      {filterKey && (
        <div className="mt-2 flex items-center justify-between gap-3 rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-800">
          <span className="min-w-0 truncate font-semibold">
            Notes mentioning @{filterName}
          </span>
          <button
            type="button"
            onClick={() => setActiveDishFilter(null)}
            className="shrink-0 font-bold hover:underline"
          >
            Show all notes
          </button>
        </div>
      )}

      {session?.user ? (
        <form onSubmit={submit} className="relative mt-2">
          <textarea
            ref={textareaRef}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={2}
            maxLength={1000}
            placeholder='Share a note, review, or thought — type "@" to point at a dish (e.g. "@Vegan Tacos ask for no crema")'
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
              Be kind; dish notes help everyone.
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
        <div className="mt-2 rounded-xl border border-emerald-100 bg-emerald-50/60 p-3">
          <div className="text-sm font-bold text-stone-800">
            Join the conversation
          </div>
          <p className="mt-0.5 text-xs text-stone-500">
            {GOOGLE_AUTH_ENABLED
              ? "Continue with Google, or use your email to get a secure sign-in link."
              : "Use your email to get a secure sign-in link and join the conversation."}
          </p>
          {initialMention && (
            <p className="mt-1 text-xs font-semibold text-sky-700">
              After signing in, your note will mention @{
                typeof initialMention === "string"
                  ? initialMention
                  : initialMention.name
              }.
            </p>
          )}
          {authSent ? (
            <div className="mt-3 rounded-lg bg-white px-3 py-2 text-xs text-stone-600">
              <div className="font-bold text-emerald-700">Check your email 📬</div>
              <p className="mt-0.5">
                We sent a sign-in link to <span className="font-semibold">{email}</span>.
                Open it on this device to finish signing in.
              </p>
            </div>
          ) : (
            <div className="mt-3">
              {GOOGLE_AUTH_ENABLED && (
                <button
                  type="button"
                  onClick={continueWithGoogle}
                  disabled={authBusy}
                  className="flex w-full items-center justify-center gap-2 rounded-lg border border-stone-300 bg-white py-2 text-sm font-bold text-stone-700 hover:bg-stone-50 disabled:text-stone-300"
                >
                  <span aria-hidden>G</span> Continue with Google
                </button>
              )}
              {GOOGLE_AUTH_ENABLED && (
                <div className="my-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-wide text-stone-400">
                  <span className="h-px flex-1 bg-emerald-100" />
                  or use your email
                  <span className="h-px flex-1 bg-emerald-100" />
                </div>
              )}
              <form
                onSubmit={sendSignInLink}
                className="flex gap-2 max-sm:flex-col"
              >
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  aria-label="Email address"
                  className="min-w-0 flex-1 rounded-lg border border-stone-300 bg-white px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
                />
                <button
                  type="submit"
                  disabled={authBusy}
                  className="shrink-0 rounded-lg bg-emerald-700 px-3 py-2 text-xs font-bold text-white hover:bg-emerald-800 disabled:bg-stone-300"
                >
                  {authBusy ? "Sending…" : "Email sign-in link"}
                </button>
              </form>
            </div>
          )}
          {authError && (
            <div className="mt-2 text-xs text-rose-600">{authError}</div>
          )}
        </div>
      )}
      {error && <div className="mt-2 text-xs text-rose-600">{error}</div>}

      <div className="mt-3 space-y-3">
        {comments === null && (
          <div className="text-xs text-stone-400">Loading notes…</div>
        )}
        {comments?.length === 0 && (
          <div className="text-xs text-stone-400">
            No notes yet — be the first.
          </div>
        )}
        {filterKey && comments?.length > 0 && visibleComments.length === 0 && (
          <div className="text-xs text-stone-400">
            No notes mention this dish yet.
          </div>
        )}
        {visibleComments.map((comment) => (
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
