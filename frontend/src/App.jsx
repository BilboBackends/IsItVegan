import { useEffect, useMemo, useState } from "react";

// The pipeline dashboard. Talks only to our own backend (proxied /api/*),
// so no keys ever reach the browser. Phase 0 shows discovered restaurants;
// later phases will add dishes + verdicts to this same view.

function StatCard({ label, value, hint }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-2xl font-semibold text-slate-900">{value}</div>
      <div className="text-sm font-medium text-slate-600">{label}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </div>
  );
}

export default function App() {
  const [restaurants, setRestaurants] = useState([]);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [enriching, setEnriching] = useState(false);
  const [notice, setNotice] = useState(null);
  const [query, setQuery] = useState("");
  const [menuFor, setMenuFor] = useState(null); // restaurant whose menu is open
  const [menuText, setMenuText] = useState(null);
  const [menuScore, setMenuScore] = useState(null);
  const [menuLoading, setMenuLoading] = useState(false);

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const [rRes, cRes] = await Promise.all([
        fetch("/api/restaurants"),
        fetch("/api/config"),
      ]);
      if (!rRes.ok) throw new Error(`/api/restaurants ${rRes.status}`);
      const rData = await rRes.json();
      setRestaurants(rData.restaurants);
      if (cRes.ok) setConfig(await cRes.json());
    } catch (e) {
      setError(e.message || "Failed to load. Is the backend running on :5000?");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, []);

  async function runDiscovery() {
    setDiscovering(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Discovery failed (${res.status})`);
      setNotice(
        `Discovery complete: ${data.discovered} restaurant(s) in ${
          config?.city || "the area"
        }. Total in DB: ${data.total_in_db}.`
      );
      await loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setDiscovering(false);
    }
  }

  async function runIngest() {
    setIngesting(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Ingest failed (${res.status})`);
      setNotice(
        `Menu ingestion: ${data.succeeded} scraped, ${data.failed} failed ` +
          `(blocked / JS-rendered — photo-fallback candidates).`
      );
      await loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setIngesting(false);
    }
  }

  async function runEnrich() {
    setEnriching(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/enrich", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Enrich failed (${res.status})`);
      setNotice(
        `Google enrichment: ${data.veg_yes} vegetarian-friendly, ` +
          `${data.veg_unknown} unknown; ${data.with_editorial} editorial summaries.`
      );
      await loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setEnriching(false);
    }
  }

  async function openMenu(r) {
    setMenuFor(r);
    setMenuText(null);
    setMenuLoading(true);
    try {
      const res = await fetch(`/api/restaurants/${r.id}/menu-text`);
      const data = await res.json();
      if (res.ok) {
        setMenuText(data.content);
        setMenuScore(data.menu_score);
      } else {
        setMenuText(`(${data.error || "no text"})`);
        setMenuScore(null);
      }
    } catch (e) {
      setMenuText(`(failed to load: ${e.message})`);
    } finally {
      setMenuLoading(false);
    }
  }

  // Data-quality flags: places with no website (scraping fallback needed)
  // and how many have menu text scraped so far.
  const noWebsite = useMemo(
    () => restaurants.filter((r) => !r.website_url).length,
    [restaurants]
  );
  const withMenuText = useMemo(
    () => restaurants.filter((r) => r.has_menu_text).length,
    [restaurants]
  );
  const vegFriendly = useMemo(
    () => restaurants.filter((r) => r.serves_vegetarian === 1).length,
    [restaurants]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return restaurants;
    return restaurants.filter(
      (r) =>
        r.name?.toLowerCase().includes(q) ||
        r.address?.toLowerCase().includes(q)
    );
  }, [restaurants, query]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <div className="mx-auto max-w-5xl px-4 py-8">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">VeganFind — Pipeline Dashboard</h1>
            <p className="text-sm text-slate-500">
              Phase 0: restaurant discovery
              {config?.city ? ` · ${config.city}, FL` : ""}
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={runEnrich}
              disabled={enriching || !config?.has_api_key}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
              title="Pull Google's food signals (vegetarian-friendly, editorial summary)"
            >
              {enriching ? "Enriching…" : "Enrich (Google)"}
            </button>
            <button
              onClick={runIngest}
              disabled={ingesting || discovering}
              className="rounded-lg border border-emerald-600 px-4 py-2 text-sm font-semibold text-emerald-700 shadow-sm transition hover:bg-emerald-50 disabled:cursor-not-allowed disabled:border-slate-300 disabled:text-slate-400"
              title="Scrapes menu text for restaurants that don't have it yet"
            >
              {ingesting ? "Ingesting…" : "Ingest menus"}
            </button>
            <button
              onClick={runDiscovery}
              disabled={discovering || ingesting || !config?.has_api_key}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              title={
                config && !config.has_api_key
                  ? "GOOGLE_PLACES_API_KEY not set in .env"
                  : "Runs ~49 Places API calls"
              }
            >
              {discovering ? "Discovering…" : "Run discovery"}
            </button>
          </div>
        </header>

        {config && !config.has_api_key && (
          <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            No <code>GOOGLE_PLACES_API_KEY</code> in <code>.env</code> — discovery
            is disabled. You can still browse existing data.
          </div>
        )}
        {notice && (
          <div className="mb-4 rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
            {notice}
          </div>
        )}
        {error && (
          <div className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Restaurants" value={restaurants.length} />
          <StatCard
            label="Real menus found"
            value={withMenuText}
            hint="passed menu detection"
          />
          <StatCard
            label="Vegetarian-friendly"
            value={vegFriendly}
            hint="per Google"
          />
          <StatCard
            label="No usable menu"
            value={restaurants.length - withMenuText}
            hint="blocked / JS / homepage-only"
          />
        </div>

        <div className="mb-3 flex items-center justify-between gap-3">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name or address…"
            className="w-full max-w-sm rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
          />
          <span className="whitespace-nowrap text-sm text-slate-500">
            {filtered.length} shown
          </span>
        </div>

        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          {loading ? (
            <div className="p-8 text-center text-slate-400">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="p-8 text-center text-slate-400">
              No restaurants. Click “Run discovery” to populate.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-3 font-medium">Name</th>
                    <th className="px-4 py-3 font-medium">Veg?</th>
                    <th className="px-4 py-3 font-medium">Address</th>
                    <th className="px-4 py-3 font-medium">Website</th>
                    <th className="px-4 py-3 font-medium">Menu text</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {filtered.map((r) => (
                    <tr key={r.place_id} className="hover:bg-slate-50">
                      <td
                        className="px-4 py-3 font-medium text-slate-900"
                        title={r.editorial_summary || ""}
                      >
                        {r.name}
                        {r.editorial_summary && (
                          <span className="ml-1 text-slate-300" title={r.editorial_summary}>
                            ⓘ
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {r.serves_vegetarian === 1 ? (
                          <span className="text-emerald-600" title="Google: serves vegetarian food">
                            ✓
                          </span>
                        ) : r.serves_vegetarian === 0 ? (
                          <span className="text-slate-400" title="Google: does not serve vegetarian food">
                            ✗
                          </span>
                        ) : (
                          <span className="text-slate-300" title="Unknown">
                            ?
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-slate-600">{r.address}</td>
                      <td className="px-4 py-3">
                        {r.website_url ? (
                          <a
                            href={r.website_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-emerald-600 hover:underline"
                          >
                            visit
                          </a>
                        ) : (
                          <span className="text-slate-300">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {r.has_menu_text ? (
                          <button
                            onClick={() => openMenu(r)}
                            className="rounded bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 hover:bg-emerald-100"
                          >
                            view
                          </button>
                        ) : (
                          <span className="text-xs text-slate-400">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {menuFor && (
        <div
          className="fixed inset-0 z-10 flex items-center justify-center bg-slate-900/40 p-4"
          onClick={() => setMenuFor(null)}
        >
          <div
            className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-xl bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h2 className="font-semibold text-slate-900">
                Scraped menu — {menuFor.name}
                {menuScore != null && (
                  <span className="ml-2 rounded bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700">
                    menu score {menuScore.toFixed(2)}
                  </span>
                )}
              </h2>
              <button
                onClick={() => setMenuFor(null)}
                className="text-slate-400 hover:text-slate-700"
              >
                ✕
              </button>
            </div>
            <div className="overflow-y-auto p-4">
              {menuLoading ? (
                <div className="text-slate-400">Loading…</div>
              ) : (
                <pre className="whitespace-pre-wrap break-words font-mono text-xs text-slate-700">
                  {menuText}
                </pre>
              )}
            </div>
            <div className="border-t border-slate-200 px-4 py-2 text-xs text-slate-400">
              Raw text — Claude will parse dishes from this in Phase 3.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
