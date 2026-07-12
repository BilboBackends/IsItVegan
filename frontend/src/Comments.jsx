import { useContext, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  CLOUD_ENABLED,
  GOOGLE_AUTH_ENABLED,
  ProfileContext,
  SessionContext,
  deleteComment,
  dishKey,
  fetchComments,
  postComment,
  rememberCommentAuthReturn,
  reportComment,
  searchUsernames,
  signInWithGoogle,
  signInWithMagicLink,
} from "./cloud.js";
import {
  escapeRegExp,
  hasDishMentionToken,
  hasUserMentionToken,
  mentionTriggerAt,
  replaceMentionTrigger,
  resolveUserMention,
} from "./mentionText.js";
import { replyPreview } from "./commentReplies.js";
import { DEFAULT_PUBLIC_NAME } from "./username.js";

// Per-restaurant discussion thread. Typing "@" in the composer suggests the
// restaurant's dishes and public usernames. Dish and user mentions are stored
// separately so dishes survive pipeline renumbering and people survive a
// later username change. Signed-in users only can post (RLS enforces it).
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
  usernameSearch = searchUsernames,
}) {
  const session = useContext(SessionContext);
  const profile = useContext(ProfileContext);
  const controlled = controlledComments !== undefined;
  const [ownComments, setOwnComments] = useState(null);
  const comments = controlled ? controlledComments : ownComments;
  const setComments = controlled ? onCommentsChange : setOwnComments;
  const [body, setBody] = useState("");
  const [mentions, setMentions] = useState([]); // [{dish_key, dish_name}]
  const [userMentions, setUserMentions] = useState([]); // [{user_id, username}]
  const [replyTarget, setReplyTarget] = useState(null);
  const [usernameSearchState, setUsernameSearchState] = useState({
    query: null,
    results: [],
  });
  const [mentionTrigger, setMentionTrigger] = useState(null);
  const [activeSuggestion, setActiveSuggestion] = useState(0);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [authSent, setAuthSent] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [activeDishFilter, setActiveDishFilter] = useState(filterDish);
  const textareaRef = useRef(null);
  const mentionListId = useId();

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
    setReplyTarget(null);
  }, [placeId]);

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

  // The active trigger follows the caret, so adding a mention in the middle
  // of an existing note preserves everything after it.
  const mentionQuery = mentionTrigger?.query.toLowerCase() ?? null;
  const usernameQuery =
    mentionQuery != null && /^[a-z0-9_]*$/.test(mentionQuery)
      ? mentionQuery
      : null;
  const usernameResults =
    usernameSearchState.query === usernameQuery
      ? usernameSearchState.results
      : [];

  useEffect(() => {
    if (!session?.user || usernameQuery == null) {
      setUsernameSearchState({ query: null, results: [] });
      return undefined;
    }
    let cancelled = false;
    setUsernameSearchState({ query: usernameQuery, results: [] });
    const timer = window.setTimeout(() => {
      usernameSearch(usernameQuery)
        .then(
          (rows) =>
            !cancelled &&
            setUsernameSearchState({ query: usernameQuery, results: rows })
        )
        .catch(
          () =>
            !cancelled &&
            setUsernameSearchState({ query: usernameQuery, results: [] })
        );
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [session?.user, usernameQuery, usernameSearch]);

  const suggestions = useMemo(() => {
    if (mentionQuery == null) return [];
    const people = usernameResults.slice(0, 4).map((item) => ({
      kind: "user",
      key: `user:${item.id}`,
      user_id: item.id,
      username: item.username,
    }));
    const dishNames = [...new Set((dishes || []).map((dish) => dish.name))];
    const dishMatches = dishNames
      .filter((name) => name.toLowerCase().includes(mentionQuery.trim()))
      .slice(0, 4)
      .map((name) => ({
        kind: "dish",
        key: `dish:${dishKey(name)}`,
        dish_name: name,
        dish_key: dishKey(name),
      }));
    return [...people, ...dishMatches].slice(0, 8);
  }, [mentionQuery, usernameResults, dishes]);

  useEffect(() => {
    setActiveSuggestion(0);
  }, [mentionQuery, suggestions.length]);

  function refreshMentionTrigger(value, caret) {
    setMentionTrigger(mentionTriggerAt(value, caret));
  }

  function acceptSuggestion(suggestion) {
    if (!suggestion || !mentionTrigger) return;
    const label =
      suggestion.kind === "user"
        ? suggestion.username
        : suggestion.dish_name;
    const replaced = replaceMentionTrigger(body, mentionTrigger, label);
    setBody(replaced.text);
    if (suggestion.kind === "user") {
      setUserMentions((current) =>
        current.some((item) => item.user_id === suggestion.user_id)
          ? current
          : [
              ...current,
              {
                user_id: suggestion.user_id,
                username: suggestion.username,
              },
            ]
      );
    } else {
      setMentions((current) =>
        current.some((item) => item.dish_key === suggestion.dish_key)
          ? current
          : [
              ...current,
              {
                dish_key: suggestion.dish_key,
                dish_name: suggestion.dish_name,
              },
            ]
      );
    }
    setMentionTrigger(null);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(replaced.caret, replaced.caret);
    });
  }

  function mentionUser(userId, username) {
    if (!session?.user || !userId || !username) return;
    const token = `@${username}`;
    const alreadyMentioned = hasUserMentionToken(body, username);
    if (!alreadyMentioned) {
      setBody(body ? `${token} ${body}` : `${token} `);
    }
    setUserMentions((current) =>
      current.some((item) => item.user_id === userId)
        ? current
        : [...current, { user_id: userId, username }]
    );
    setMentionTrigger(null);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      if (!alreadyMentioned) {
        textareaRef.current?.setSelectionRange(token.length + 1, token.length + 1);
      }
    });
  }

  function startReply(comment) {
    if (!session?.user || !comment?.id) return;
    setReplyTarget(comment);
    setMentionTrigger(null);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    });
  }

  function scrollToComment(commentId) {
    const commentElement = document.getElementById(`note-${commentId}`);
    if (!commentElement) return;
    commentElement.scrollIntoView({ behavior: "smooth", block: "center" });
    commentElement.focus({ preventScroll: true });
  }

  function handleComposerKeyDown(event) {
    if (!mentionTrigger || suggestions.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveSuggestion((current) => (current + 1) % suggestions.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveSuggestion(
        (current) => (current - 1 + suggestions.length) % suggestions.length
      );
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      acceptSuggestion(suggestions[activeSuggestion]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setMentionTrigger(null);
    }
  }

  async function submit(event) {
    event.preventDefault();
    const text = body.trim();
    if (!text || busy || !session?.user) return;
    setBusy(true);
    setError(null);
    const parent = replyTarget;
    try {
      // Keep only mentions still present in the final text.
      const keptDishes = mentions.filter((mention) =>
        hasDishMentionToken(text, mention.dish_name)
      );
      const keptUsers = userMentions.filter((mention) =>
        hasUserMentionToken(text, mention.username)
      );
      const row = await postComment(placeId, text, {
        dishMentions: keptDishes,
        userMentions: keptUsers,
        userId: session.user.id,
        parentCommentId: parent?.id || null,
      });
      const completedRow = parent ? { ...row, reply_to: parent } : row;
      setComments((current) => [completedRow, ...(current || [])]);
      setBody("");
      setMentions([]);
      setUserMentions([]);
      setReplyTarget(null);
      setMentionTrigger(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    try {
      await deleteComment(id);
      setComments((current) =>
        (current || [])
          .filter((comment) => comment.id !== id)
          .map((comment) =>
            comment.parent_comment_id === id
              ? { ...comment, reply_to: null }
              : comment
          )
      );
      setReplyTarget((current) => (current?.id === id ? null : current));
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

  // Only structured selections become interactive. Raw @text stays plain,
  // while legacy dish mentions continue to work unchanged.
  function renderBody(comment) {
    const descriptors = [
      ...(comment.user_mentions || [])
        .filter((mention) => mention?.user_id && mention?.username)
        .map((mention) => {
          const resolved = resolveUserMention({
            ...mention,
            ...(profile?.id === mention.user_id
              ? { current_username: profile.username || null }
              : {}),
          });
          return {
            kind: "user",
            ...resolved,
            user_id: mention.user_id,
          };
        }),
      ...(comment.mentions || [])
        .filter((mention) => mention?.dish_name)
        .map((mention) => ({
          kind: "dish",
          token: `@${mention.dish_name}`,
          dish_name: mention.dish_name,
        })),
    ].sort((a, b) => b.token.length - a.token.length);
    if (descriptors.length === 0) return comment.body;
    const pattern = new RegExp(
      `(${descriptors.map((item) => escapeRegExp(item.token)).join("|")})`,
      "gi"
    );
    const parts = comment.body.split(pattern);
    return parts.map((part, i) => {
      const descriptor =
        descriptors.find((item) => item.token === part) ||
        descriptors.find(
          (item) =>
            item.kind === "user" &&
            item.token.toLowerCase() === part.toLowerCase()
        );
      if (!descriptor) return part;
      if (descriptor.kind === "user") {
        if (!session?.user || !descriptor.mentionUsername) {
          return (
            <span
              key={`${descriptor.token}-${i}`}
              title={
                descriptor.mentionUsername
                  ? undefined
                  : "This username is no longer active"
              }
              className="mx-0.5 inline-flex items-center rounded-full bg-sky-50 px-2 py-0.5 text-xs font-bold text-sky-700"
            >
              @{descriptor.displayUsername}
            </span>
          );
        }
        return (
          <button
            type="button"
            key={`${descriptor.token}-${i}`}
            onClick={() =>
              mentionUser(descriptor.user_id, descriptor.mentionUsername)
            }
            title={`Mention @${descriptor.mentionUsername}`}
            className="mx-0.5 inline-flex items-center rounded-full bg-sky-50 px-2 py-0.5 text-xs font-bold text-sky-700 hover:bg-sky-100"
          >
            @{descriptor.displayUsername}
          </button>
        );
      }
      const dish = (dishes || []).find(
        (item) => item.name === descriptor.dish_name
      );
      return (
        <button
          type="button"
          key={`${descriptor.token}-${i}`}
          onClick={() => dish && onOpenDish?.(dish)}
          className="mx-0.5 inline-flex items-center rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-800 hover:bg-emerald-100"
        >
          🍽 {descriptor.dish_name}
        </button>
      );
    });
  }

  function authorUsername(comment) {
    if (profile?.id === comment.user_id) return profile.username || null;
    return comment.profiles?.username || null;
  }

  function authorLabel(comment) {
    const username = authorUsername(comment);
    return username ? `@${username}` : DEFAULT_PUBLIC_NAME;
  }

  function renderReplyContext(comment) {
    if (!comment.parent_comment_id) return null;
    if (!comment.reply_to) {
      return (
        <div className="mb-1.5 text-[11px] font-semibold text-stone-400">
          ↪ Original note unavailable
        </div>
      );
    }
    const context = (
      <>
        <span className="font-bold text-sky-700">
          ↪ Replying to {authorLabel(comment.reply_to)}
        </span>
        <span className="min-w-0 truncate text-stone-500">
          “{replyPreview(comment.reply_to.body)}”
        </span>
      </>
    );
    const parentIsVisible = visibleComments.some(
      (candidate) => candidate.id === comment.parent_comment_id
    );
    return parentIsVisible ? (
      <button
        type="button"
        onClick={() => scrollToComment(comment.parent_comment_id)}
        className="mb-1.5 flex max-w-full items-center gap-1.5 text-left text-[11px] hover:underline"
        title="Go to the original note"
      >
        {context}
      </button>
    ) : (
      <div className="mb-1.5 flex max-w-full items-center gap-1.5 text-[11px]">
        {context}
      </div>
    );
  }

  if (!CLOUD_ENABLED || !placeId) return null;

  const filterName =
    typeof activeDishFilter === "string"
      ? activeDishFilter
      : activeDishFilter?.name;
  const filterKey = filterName ? dishKey(filterName) : null;
  const commentsById = new Map(
    (comments || []).map((comment) => [comment.id, comment])
  );
  function threadMentionsDish(comment, seen = new Set()) {
    if (
      (comment?.mentions || []).some(
        (mention) => mention.dish_key === filterKey
      )
    ) {
      return true;
    }
    if (!comment?.parent_comment_id || seen.has(comment.parent_comment_id)) {
      return false;
    }
    seen.add(comment.parent_comment_id);
    return threadMentionsDish(
      comment.reply_to || commentsById.get(comment.parent_comment_id),
      seen
    );
  }
  const visibleComments = filterKey
    ? (comments || []).filter((comment) => threadMentionsDish(comment))
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
          {replyTarget && (
            <div className="mb-2 flex items-start justify-between gap-3 rounded-xl border border-sky-100 bg-sky-50 px-3 py-2">
              <div className="min-w-0">
                <div className="text-xs font-bold text-sky-800">
                  Replying to {authorLabel(replyTarget)}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-stone-500">
                  “{replyPreview(replyTarget.body)}”
                </div>
              </div>
              <button
                type="button"
                onClick={() => setReplyTarget(null)}
                aria-label="Cancel reply"
                className="shrink-0 rounded-full px-1.5 text-sm font-bold text-stone-400 hover:bg-white hover:text-stone-700"
              >
                ×
              </button>
            </div>
          )}
          <div className="relative">
            <textarea
              ref={textareaRef}
              value={body}
              onChange={(event) => {
                setBody(event.target.value);
                refreshMentionTrigger(
                  event.target.value,
                  event.target.selectionStart
                );
              }}
              onClick={(event) =>
                refreshMentionTrigger(body, event.currentTarget.selectionStart)
              }
              onKeyUp={(event) => {
                if (
                  ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(
                    event.key
                  )
                ) {
                  return;
                }
                refreshMentionTrigger(body, event.currentTarget.selectionStart);
              }}
              onKeyDown={handleComposerKeyDown}
              rows={2}
              maxLength={1000}
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={Boolean(mentionTrigger && suggestions.length > 0)}
              aria-controls={
                mentionTrigger && suggestions.length > 0
                  ? mentionListId
                  : undefined
              }
              aria-activedescendant={
                mentionTrigger && suggestions[activeSuggestion]
                  ? `${mentionListId}-${activeSuggestion}`
                  : undefined
              }
              placeholder='Share a note — type "@" to mention a person or dish'
              className="w-full rounded-xl border border-stone-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
            />
            {mentionTrigger && suggestions.length > 0 && (
              <div
                id={mentionListId}
                role="listbox"
                aria-label="Mention suggestions"
                className="absolute left-0 top-full z-30 mt-1 w-80 max-w-full overflow-hidden rounded-xl border border-stone-200 bg-white shadow-lg"
              >
                {suggestions.map((suggestion, index) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === activeSuggestion}
                    id={`${mentionListId}-${index}`}
                    key={suggestion.key}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => acceptSuggestion(suggestion)}
                    className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm ${
                      index === activeSuggestion
                        ? "bg-emerald-50 text-stone-900"
                        : "text-stone-700 hover:bg-stone-50"
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-extrabold ${
                        suggestion.kind === "user"
                          ? "bg-sky-100 text-sky-700"
                          : "bg-emerald-100 text-emerald-700"
                      }`}
                    >
                      {suggestion.kind === "user" ? "@" : "🍽"}
                    </span>
                    <span className="min-w-0 flex-1 truncate font-semibold">
                      @{suggestion.kind === "user"
                        ? suggestion.username
                        : suggestion.dish_name}
                    </span>
                    <span className="shrink-0 text-[10px] font-bold uppercase tracking-wide text-stone-400">
                      {suggestion.kind === "user" ? "Person" : "Dish"}
                    </span>
                  </button>
                ))}
              </div>
            )}
            <span className="sr-only" aria-live="polite">
              {mentionTrigger
                ? `${suggestions.length} mention suggestion${suggestions.length === 1 ? "" : "s"}`
                : ""}
            </span>
          </div>
          <div className="mt-1.5 flex items-center justify-between">
            <span className="text-[11px] text-stone-400">
              Use @ to mention a person or dish. Be kind.
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
          <div
            id={`note-${comment.id}`}
            key={comment.id}
            tabIndex={-1}
            className={`rounded-xl bg-stone-50 px-3 py-2 outline-none transition focus:ring-2 focus:ring-sky-200 ${
              comment.parent_comment_id ? "ml-3 border-l-2 border-sky-100" : ""
            }`}
          >
            {renderReplyContext(comment)}
            <div className="flex items-baseline justify-between gap-2">
              {authorUsername(comment) &&
              session?.user &&
              comment.user_id !== session.user.id ? (
                <button
                  type="button"
                  onClick={() =>
                    mentionUser(comment.user_id, authorUsername(comment))
                  }
                  title={`Mention @${authorUsername(comment)}`}
                  className="text-xs font-bold text-sky-700 hover:underline"
                >
                  @{authorUsername(comment)}
                </button>
              ) : (
                <span className="text-xs font-bold text-stone-700">
                  {authorUsername(comment)
                    ? `@${authorUsername(comment)}`
                    : DEFAULT_PUBLIC_NAME}
                </span>
              )}
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
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => startReply(comment)}
                  className="text-sky-600 hover:text-sky-800 disabled:text-stone-300"
                >
                  Reply
                </button>
                {comment.user_id === session.user.id ? (
                  <button
                    type="button"
                    onClick={() => remove(comment.id)}
                    className="text-stone-400 hover:text-rose-600"
                  >
                    Delete
                  </button>
                ) : (
                  <button
                    type="button"
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
