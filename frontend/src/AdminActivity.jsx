import { useEffect, useMemo, useState } from "react";

// Admin › User activity: everything the community does on the public site
// (sign-ups, notes, replies, thumbs, favorites, reports), pulled from the
// Supabase user-data plane by the LOCAL backend with the service role key.
// This page never ships in the static build — see App.jsx routing.

const FEED_FILTERS = [
  { key: "all", label: "All" },
  { key: "signup", label: "Sign-ups" },
  { key: "note", label: "Notes" },
  { key: "reply", label: "Replies" },
  { key: "vote", label: "Votes" },
  { key: "favorite", label: "Favorites" },
  { key: "report", label: "Reports" },
];

const EVENT_META = {
  signup: { icon: "🎉", label: "joined DishTune" },
  note: { icon: "💬", label: "left a note" },
  reply: { icon: "↩️", label: "replied" },
  vote: { icon: "👍", label: "voted" },
  favorite: { icon: "❤️", label: "favorited" },
  report: { icon: "🚩", label: "reported a note" },
};

function timeAgo(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  return new Date(iso).toLocaleDateString();
}

function fullTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}

function withinDays(iso, days) {
  if (!iso) return false;
  const then = new Date(iso).getTime();
  return !Number.isNaN(then) && Date.now() - then <= days * 86_400_000;
}

// Actors may have chosen a public username; accounts without one stay the
// product's inclusive default instead of leaking their email into the feed.
function actorName(item) {
  return item.username ? `@${item.username}` : "a dish explorer";
}

function StatTile({ label, value, delta }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">
        {value.toLocaleString()}
      </div>
      <div className="mt-0.5 text-xs text-slate-400">
        {delta > 0 ? (
          <span className="font-semibold text-emerald-700">+{delta}</span>
        ) : (
          "none"
        )}{" "}
        in the last 7 days
      </div>
    </div>
  );
}

export default function AdminActivity() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [filter, setFilter] = useState("all");
  const [feedLimit, setFeedLimit] = useState(50);

  async function load() {
    setError(null);
    try {
      const response = await fetch("/api/admin/activity");
      if (response.status === 404) {
        // The backend keeps use_reloader=False, so a route added after it
        // started 404s (as Flask's HTML error page) until a manual restart.
        throw new Error(
          "The backend doesn't know this endpoint yet — restart `python api.py` and refresh."
        );
      }
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        throw new Error(`Backend returned a non-JSON response (${response.status}).`);
      }
      if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
      setData(payload);
      setRefreshedAt(new Date());
    } catch (e) {
      setError(e.message || "Failed to load. Is the backend running on :5000?");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, []);

  // One chronological feed across every activity kind. Each source list is
  // already capped server-side, so this stays small.
  const feed = useMemo(() => {
    if (!data?.enabled) return [];
    const events = [];
    for (const user of data.users) {
      events.push({
        type: "signup",
        at: user.created_at,
        actor: user.username ? `@${user.username}` : user.email,
        detail: user.provider ? `via ${user.provider}` : null,
      });
    }
    for (const comment of data.comments) {
      events.push({
        type: comment.parent_comment_id ? "reply" : "note",
        at: comment.created_at,
        actor: actorName(comment),
        target: comment.restaurant_name || comment.place_id,
        detail: comment.parent_comment_id
          ? `to ${comment.parent_username ? `@${comment.parent_username}` : "an earlier note"}`
          : null,
        body: comment.body,
      });
    }
    for (const vote of data.votes) {
      events.push({
        type: "vote",
        at: vote.updated_at,
        actor: actorName(vote),
        icon: vote.vote === "down" ? "👎" : "👍",
        target: vote.restaurant_name || vote.place_id,
        detail:
          vote.kind === "dish"
            ? `${vote.vote} on ${vote.dish_name || vote.dish_key}`
            : `${vote.vote} on the restaurant`,
      });
    }
    for (const favorite of data.favorites) {
      events.push({
        type: "favorite",
        at: favorite.created_at,
        actor: actorName(favorite),
        target: favorite.restaurant_name || favorite.place_id,
        detail: favorite.kind === "dish" ? favorite.dish_name || favorite.dish_key : null,
      });
    }
    for (const report of data.reports) {
      events.push({
        type: "report",
        at: report.created_at,
        actor: actorName(report),
        body: report.comment_body,
      });
    }
    return events.sort((a, b) => (b.at || "").localeCompare(a.at || ""));
  }, [data]);

  const shownFeed = useMemo(
    () => (filter === "all" ? feed : feed.filter((e) => e.type === filter)),
    [feed, filter]
  );

  const stats = useMemo(() => {
    if (!data?.enabled) return null;
    const notes = data.comments.filter((c) => !c.parent_comment_id);
    const replies = data.comments.filter((c) => c.parent_comment_id);
    const week = (list, field = "created_at") =>
      list.filter((item) => withinDays(item[field], 7)).length;
    return [
      { label: "Accounts", value: data.users.length, delta: week(data.users) },
      { label: "Notes", value: notes.length, delta: week(notes) },
      { label: "Replies", value: replies.length, delta: week(replies) },
      { label: "Votes", value: data.votes.length, delta: week(data.votes, "updated_at") },
      { label: "Favorites", value: data.favorites.length, delta: week(data.favorites) },
      { label: "Reports", value: data.reports.length, delta: week(data.reports) },
    ];
  }, [data]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <div className="mx-auto w-full max-w-6xl px-2 py-4 sm:px-6 sm:py-8">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-lg font-bold sm:text-2xl">DishTune — User Activity</h1>
            <p className="text-sm text-slate-500">
              Sign-ups · notes &amp; replies · votes · favorites · reports
            </p>
          </div>
          <div className="flex items-center gap-2">
            <a
              href="#admin"
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50"
            >
              ← Pipeline
            </a>
            <button
              onClick={() => {
                setLoading(true);
                load();
              }}
              disabled={loading}
              className="rounded-lg bg-slate-800 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-700 disabled:bg-slate-300"
            >
              {loading ? "Refreshing…" : "↻ Refresh"}
            </button>
          </div>
        </header>

        {error && (
          <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        {loading && !data ? (
          <div className="p-10 text-center text-slate-400">Loading…</div>
        ) : data && !data.enabled ? (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-6">
            <div className="text-sm font-bold text-amber-900">
              Activity monitoring is not configured yet
            </div>
            <p className="mt-2 text-sm text-amber-800">
              This page reads the account backend (Supabase) with the service
              role key, which must stay server-side. Add the missing values to{" "}
              <code className="rounded bg-amber-100 px-1">.env</code> in the
              project root and restart the backend:
            </p>
            <pre className="mt-3 rounded-lg bg-white/70 p-3 text-xs text-amber-900">
              {(data.missing || []).map((name) => `${name}=…`).join("\n")}
            </pre>
            <p className="mt-2 text-xs text-amber-700">
              SUPABASE_URL matches VITE_SUPABASE_URL in frontend/.env.local;
              the service role key is in Supabase → Project settings → API.
              Never put it in any VITE_ variable.
            </p>
          </div>
        ) : data ? (
          <>
            {/* KPI row. Totals are within the server's per-kind window
                (latest 100–200) — accounts are exact at current scale. */}
            <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {stats.map((s) => (
                <StatTile key={s.label} {...s} />
              ))}
            </div>

            <div className="grid items-start gap-5 lg:grid-cols-5">
              {/* Recent sign-ups */}
              <section className="rounded-2xl border border-slate-200 bg-white shadow-sm lg:col-span-2">
                <div className="border-b border-slate-100 px-4 py-3 text-sm font-bold">
                  Recent sign-ups
                </div>
                {data.users.length === 0 ? (
                  <div className="p-6 text-center text-sm text-slate-400">
                    No accounts yet.
                  </div>
                ) : (
                  <ul className="divide-y divide-slate-100">
                    {data.users.slice(0, 25).map((user) => (
                      <li key={user.id} className="flex items-center gap-3 px-4 py-2.5">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-semibold text-slate-800">
                            {user.email || "(no email)"}
                          </div>
                          <div className="text-xs text-slate-400">
                            {user.username && (
                              <span className="font-semibold text-emerald-700">
                                @{user.username} ·{" "}
                              </span>
                            )}
                            {user.provider || "email"} · joined{" "}
                            <span title={fullTime(user.created_at)}>
                              {timeAgo(user.created_at)}
                            </span>
                          </div>
                        </div>
                        <div
                          className="shrink-0 text-right text-xs text-slate-400"
                          title={fullTime(user.last_sign_in_at)}
                        >
                          {user.last_sign_in_at
                            ? `seen ${timeAgo(user.last_sign_in_at)}`
                            : "never signed in"}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* Unified feed */}
              <section className="rounded-2xl border border-slate-200 bg-white shadow-sm lg:col-span-3">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
                  <div className="text-sm font-bold">Activity feed</div>
                  <div className="flex flex-wrap gap-1">
                    {FEED_FILTERS.map((f) => (
                      <button
                        key={f.key}
                        onClick={() => {
                          setFilter(f.key);
                          setFeedLimit(50);
                        }}
                        aria-pressed={filter === f.key}
                        className={`rounded-full px-2.5 py-1 text-xs font-semibold transition ${
                          filter === f.key
                            ? "bg-slate-800 text-white"
                            : "text-slate-500 hover:bg-slate-100"
                        }`}
                      >
                        {f.label}
                      </button>
                    ))}
                  </div>
                </div>
                {shownFeed.length === 0 ? (
                  <div className="p-6 text-center text-sm text-slate-400">
                    Nothing here yet.
                  </div>
                ) : (
                  <ul className="divide-y divide-slate-100">
                    {shownFeed.slice(0, feedLimit).map((event, index) => {
                      const meta = EVENT_META[event.type];
                      return (
                        <li key={`${event.type}-${event.at}-${index}`} className="flex gap-3 px-4 py-2.5">
                          <span aria-hidden="true" className="mt-0.5 text-base">
                            {event.icon || meta.icon}
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="text-sm text-slate-700">
                              <span className="font-semibold text-slate-900">
                                {event.actor}
                              </span>{" "}
                              {meta.label}
                              {event.detail && (
                                <span className="text-slate-500"> {event.detail}</span>
                              )}
                              {event.target && (
                                <>
                                  {" "}at{" "}
                                  <span className="font-semibold text-slate-800">
                                    {event.target}
                                  </span>
                                </>
                              )}
                            </div>
                            {event.body && (
                              <div className="mt-0.5 line-clamp-2 rounded-lg bg-slate-50 px-2 py-1 text-xs text-slate-600">
                                {event.body}
                              </div>
                            )}
                          </div>
                          <span
                            className="shrink-0 text-xs text-slate-400"
                            title={fullTime(event.at)}
                          >
                            {timeAgo(event.at)}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                )}
                {shownFeed.length > feedLimit && (
                  <button
                    onClick={() => setFeedLimit((n) => n + 50)}
                    className="w-full border-t border-slate-100 px-4 py-2.5 text-sm font-semibold text-slate-500 transition hover:bg-slate-50"
                  >
                    Show more ({shownFeed.length - feedLimit} older)
                  </button>
                )}
              </section>
            </div>

            {refreshedAt && (
              <div className="mt-4 text-center text-xs text-slate-400">
                Auto-refreshes every minute · last updated{" "}
                {refreshedAt.toLocaleTimeString()}
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  );
}
