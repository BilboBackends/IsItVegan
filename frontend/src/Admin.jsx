import { useEffect, useMemo, useState } from "react";
import DishModal from "./DishModal.jsx";
import RatingBadge from "./RatingBadge.jsx";
import { FreshnessBadge, OpenStatusBadge, isMenuStale } from "./RestaurantMeta.jsx";

// The pipeline dashboard (admin view). Talks only to our own backend
// (proxied /api/*), so no keys ever reach the browser. The consumer-facing
// view lives in Explore.jsx.

function StatCard({ label, value, hint }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-2xl font-semibold text-slate-900">{value}</div>
      <div className="text-sm font-medium text-slate-600">{label}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </div>
  );
}

// Live progress for a background pipeline job (bulk scrape / bulk classify).
// `job` is the polled /api/<job>/status payload; classify jobs also carry a
// running API-cost total and per-restaurant costs.
function JobProgressPanel({ job, title }) {
  if (!job?.running) return null;
  return (
    <section className="mb-6 rounded-xl border border-emerald-200 bg-white p-4 shadow-sm">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="font-semibold text-slate-900">
          {title}…{" "}
          {job.total != null ? `${job.done} of ${job.total}` : "preparing"}
        </span>
        <span className="text-slate-500">
          <span className="font-semibold text-emerald-700">{job.succeeded} ok</span>
          {" · "}
          <span className={job.failed > 0 ? "font-semibold text-amber-700" : ""}>
            {job.failed} failed
          </span>
          {job.cost != null && (
            <>
              {" · "}
              <span className="font-semibold text-slate-700">
                ~${job.cost.toFixed(2)} so far
              </span>
            </>
          )}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-emerald-500 transition-all duration-500"
          style={{
            width: job.total
              ? `${Math.round((job.done / job.total) * 100)}%`
              : "4%",
          }}
        />
      </div>
      {job.current && (
        <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
          now: <span className="font-medium text-slate-700">{job.current}</span>
        </div>
      )}
      {job.recent?.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs">
          {job.recent.slice(0, 5).map((r) => (
            <li key={r.name} className="flex items-baseline gap-2">
              {r.ok ? (
                <>
                  <span className="text-emerald-600">✓</span>
                  <span className="font-medium text-slate-700">{r.name}</span>
                  <span className="text-slate-400">
                    {r.dishes != null
                      ? `${r.dishes} dishes` +
                        (r.veganish != null ? `, ${r.veganish} veganish` : "") +
                        (r.cost != null ? ` · ~$${r.cost.toFixed(2)}` : "")
                      : `${r.pages} page${r.pages === 1 ? "" : "s"}, ${r.chars} chars, score ${r.score?.toFixed(2)}`}
                  </span>
                </>
              ) : (
                <>
                  <span className="text-amber-600">✗</span>
                  <span className="font-medium text-slate-700">{r.name}</span>
                  <span className="truncate text-slate-400" title={r.error}>
                    {r.error}
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function Admin() {
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
  const [dishesFor, setDishesFor] = useState(null); // restaurant whose dishes are open
  const [rowBusy, setRowBusy] = useState(null); // {id, action} while a per-row job runs
  const [addOpen, setAddOpen] = useState(false);
  const [addNames, setAddNames] = useState("");
  const [adding, setAdding] = useState(false);
  const [addResult, setAddResult] = useState(null);
  const [reports, setReports] = useState([]);
  const [ingestJob, setIngestJob] = useState(null); // live bulk-scrape status
  const [classifyJob, setClassifyJob] = useState(null); // live bulk-classify status
  const [classifying, setClassifying] = useState(false);
  const [menuQuality, setMenuQuality] = useState([]); // automated audit flags
  const [qualityOpen, setQualityOpen] = useState(false);

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const [rRes, cRes, reportRes, qualityRes] = await Promise.all([
        fetch("/api/restaurants?include_excluded=true"),
        fetch("/api/config"),
        fetch("/api/reports?status=open"),
        fetch("/api/menu-quality"),
      ]);
      if (!rRes.ok) throw new Error(`/api/restaurants ${rRes.status}`);
      const rData = await rRes.json();
      setRestaurants(rData.restaurants);
      if (cRes.ok) setConfig(await cRes.json());
      if (reportRes.ok) setReports((await reportRes.json()).reports || []);
      if (qualityRes.ok) setMenuQuality((await qualityRes.json()).findings || []);
    } catch (e) {
      setError(e.message || "Failed to load. Is the backend running on :5000?");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
    // If a bulk scrape/classify is already running (e.g. the page was
    // reloaded mid-run), pick the progress views back up.
    (async () => {
      try {
        const res = await fetch("/api/ingest/status");
        const data = await res.json();
        if (data.running) {
          setIngesting(true);
          pollIngest();
        }
        const cRes = await fetch("/api/classify/status");
        const cData = await cRes.json();
        if (cData.running) {
          setClassifying(true);
          pollClassify();
        }
      } catch {
        /* backend not up yet; loadData surfaces that */
      }
    })();
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

  // Bulk ingestion runs as a background job on the backend; poll its status
  // so the dashboard shows live scrape-by-scrape progress.
  async function pollIngest(label = "Menu ingestion") {
    for (;;) {
      let data;
      try {
        const res = await fetch("/api/ingest/status");
        data = await res.json();
      } catch {
        break; // backend went away; stop polling quietly
      }
      setIngestJob(data);
      if (!data.running) {
        if (data.error) setError(data.error);
        else if (data.summary) {
          setNotice(
            `${label}: ${data.summary.succeeded} scraped, ${data.summary.failed} failed ` +
              `(blocked / JS-rendered — photo-fallback candidates).`
          );
        }
        setIngestJob(null);
        await loadData();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    setIngesting(false);
  }

  async function pollClassify() {
    for (;;) {
      let data;
      try {
        const res = await fetch("/api/classify/status");
        data = await res.json();
      } catch {
        break;
      }
      setClassifyJob(data);
      if (!data.running) {
        if (data.error) setError(data.error);
        else if (data.summary) {
          const s = data.summary;
          setNotice(
            `Classification: ${s.ok} restaurant(s), ${s.dishes} dishes, ` +
              `~$${(s.cost ?? 0).toFixed(2)} API cost` +
              (s.failed ? `, ${s.failed} failed` : "") +
              "."
          );
        }
        setClassifyJob(null);
        await loadData();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    setClassifying(false);
  }

  async function runClassify() {
    setClassifying(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/classify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Classify failed (${res.status})`);
      await pollClassify();
    } catch (e) {
      setError(e.message);
      setClassifying(false);
    }
  }

  async function runIngest(staleOnly = false) {
    setIngesting(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(staleOnly ? { stale_days: 30 } : {}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Ingest failed (${res.status})`);
      await pollIngest(staleOnly ? "Stale menu refresh" : "Menu ingestion");
    } catch (e) {
      setError(e.message);
      setIngesting(false);
    }
  }

  async function resolveReport(id) {
    const response = await fetch(`/api/reports/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "resolved" }),
    });
    if (response.ok) setReports((current) => current.filter((report) => report.id !== id));
  }

  async function runEnrich() {
    setEnriching(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch("/api/enrich", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ all: true }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Enrich failed (${res.status})`);
      setNotice(
        `Google enrichment: ${data.veg_yes} vegetarian-friendly, ` +
          `${data.veg_unknown} unknown; ${data.with_editorial} editorial summaries; ` +
          `${data.with_rating} ratings.`
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

  async function submitAddNames() {
    const names = addNames
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (names.length === 0) return;
    setAdding(true);
    setAddResult(null);
    try {
      const res = await fetch("/api/restaurants/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Add failed (${res.status})`);
      setAddResult(data);
      await loadData();
    } catch (e) {
      setAddResult({ error: e.message });
    } finally {
      setAdding(false);
    }
  }

  async function runRowAction(r, action) {
    // action: "ingest" (rescrape menu) | "classify" (re-run Claude verdicts)
    setRowBusy({ id: r.id, action });
    setNotice(null);
    setError(null);
    try {
      const res = await fetch(`/api/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ restaurant_id: r.id }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `${action} failed (${res.status})`);
      setNotice(
        action === "ingest"
          ? `Rescraped ${r.name}: ${data.succeeded ? "menu found" : "no menu found"}${
              data.failures?.[0] ? ` — ${data.failures[0].error}` : ""
            }`
          : `Reclassified ${r.name}: ${data.dishes} dishes` +
            (data.cost != null ? ` (~$${data.cost.toFixed(2)} API cost)` : "") +
            `.`
      );
      await loadData();
    } catch (e) {
      setError(`${r.name}: ${e.message}`);
    } finally {
      setRowBusy(null);
    }
  }

  async function toggleVisibility(restaurant) {
    const hidden = !Boolean(restaurant.consumer_hidden);
    const response = await fetch(`/api/restaurants/${restaurant.id}/visibility`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden }),
    });
    if (response.ok) await loadData();
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
  const totalVeganOptions = useMemo(
    () => restaurants.reduce((sum, r) => sum + (r.vegan_options || 0), 0),
    [restaurants]
  );
  const staleMenus = useMemo(
    () => restaurants.filter((restaurant) => isMenuStale(restaurant.menu_fetched_at)).length,
    [restaurants]
  );
  const unclassified = useMemo(
    () =>
      restaurants.filter((r) => r.has_menu_text && (r.dish_count || 0) === 0)
        .length,
    [restaurants]
  );
  // Pre-run cost estimates (from menu size): everything vs. only-new.
  const classifyCostAll = useMemo(
    () =>
      restaurants.reduce((sum, r) => sum + (r.classify_estimate || 0), 0),
    [restaurants]
  );
  const classifyCostNew = useMemo(
    () =>
      restaurants
        .filter((r) => r.has_menu_text && (r.dish_count || 0) === 0)
        .reduce((sum, r) => sum + (r.classify_estimate || 0), 0),
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
      <div className="mx-auto max-w-screen-2xl px-4 py-8">
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
              onClick={() => {
                setAddOpen(true);
                setAddResult(null);
              }}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50"
              title="Add restaurants by name — resolves via Google, then scrapes"
            >
              + Add restaurants
            </button>
            <button
              onClick={runEnrich}
              disabled={enriching || !config?.has_api_key}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
              title="Refresh ratings, current opening status, and Google food signals for every restaurant"
            >
              {enriching ? "Refreshing…" : "Refresh Google data"}
            </button>
            <button
              onClick={() => runIngest(true)}
              disabled={ingesting || staleMenus === 0}
              className="rounded-lg border border-amber-300 px-4 py-2 text-sm font-semibold text-amber-800 shadow-sm transition hover:bg-amber-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
              title="Re-scrape menus last checked more than 30 days ago"
            >
              Refresh stale ({staleMenus})
            </button>
            <button
              onClick={runClassify}
              disabled={classifying || unclassified === 0}
              className="rounded-lg border border-violet-400 px-4 py-2 text-sm font-semibold text-violet-700 shadow-sm transition hover:bg-violet-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
              title={`Classify restaurants that have menu text but no dishes yet — est ~$${classifyCostNew.toFixed(2)} total, billed to your Anthropic API key`}
            >
              {classifying
                ? "Classifying…"
                : `⚡ Classify new (${unclassified}${
                    unclassified > 0 ? ` · ~$${classifyCostNew.toFixed(2)}` : ""
                  })`}
            </button>
            <button
              onClick={() => runIngest(false)}
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
          <p
            className="w-full text-right text-xs text-slate-400"
            title="Sum of per-restaurant estimates based on each menu's size; hover a row's ⚡ reclassify for its individual estimate"
          >
            re-running all {withMenuText} classifications ≈ $
            {classifyCostAll.toFixed(2)} · via{" "}
            <code className="text-slate-500">python classify.py --all</code>
          </p>
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

        <JobProgressPanel job={ingestJob} title="Scraping menus" />
        <JobProgressPanel job={classifyJob} title="Classifying dishes" />

        <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Restaurants" value={restaurants.length} />
          <StatCard
            label="Real menus found"
            value={`${withMenuText} / ${restaurants.length - noWebsite}`}
            hint="of restaurants with a website"
          />
          <StatCard
            label="Vegetarian-friendly"
            value={vegFriendly}
            hint="per Google"
          />
          <StatCard
            label="Vegan options found"
            value={totalVeganOptions}
            hint="food only — drinks excluded"
          />
        </div>

        {menuQuality.length > 0 && (
          <section className="mb-6 rounded-xl border border-orange-200 bg-orange-50 p-4">
            <button
              onClick={() => setQualityOpen((v) => !v)}
              className="flex w-full items-center justify-between text-left"
            >
              <h2 className="font-bold text-orange-950">
                Menu quality warnings
                <span className="ml-2 text-xs font-medium text-orange-700">
                  automated audit — likely false or incomplete menus
                </span>
              </h2>
              <span className="rounded-full bg-orange-200 px-2 py-0.5 text-xs font-bold text-orange-900">
                {menuQuality.length} {qualityOpen ? "▾" : "▸"}
              </span>
            </button>
            {qualityOpen && (
              <div className="mt-3 space-y-2">
                {menuQuality.map((f) => (
                  <div
                    key={f.restaurant_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-white p-3 text-sm shadow-sm"
                  >
                    <div>
                      <div className="font-bold text-slate-900">{f.name}</div>
                      <ul className="mt-0.5 text-xs text-orange-800">
                        {f.flags.map((flag) => (
                          <li key={flag}>• {flag}</li>
                        ))}
                      </ul>
                    </div>
                    <button
                      onClick={() => {
                        const r = restaurants.find((x) => x.id === f.restaurant_id);
                        if (r) runRowAction(r, "ingest");
                      }}
                      disabled={rowBusy !== null}
                      className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {rowBusy?.id === f.restaurant_id && rowBusy.action === "ingest"
                        ? "scraping…"
                        : "↻ rescrape"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {reports.length > 0 && (
          <section className="mb-6 rounded-xl border border-amber-200 bg-amber-50 p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="font-bold text-amber-950">Correction reports</h2>
              <span className="rounded-full bg-amber-200 px-2 py-0.5 text-xs font-bold text-amber-900">{reports.length} open</span>
            </div>
            <div className="space-y-2">
              {reports.map((report) => (
                <div key={report.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-white p-3 text-sm shadow-sm">
                  <div>
                    <div className="font-bold text-slate-900">
                      {report.dish_name || "Restaurant report"} · {report.restaurant_name}
                    </div>
                    <div className="mt-0.5 text-xs capitalize text-amber-800">
                      {report.issue_type.replaceAll("_", " ")}
                      {report.note && ` — ${report.note}`}
                    </div>
                  </div>
                  <button onClick={() => resolveReport(report.id)} className="rounded-lg border border-emerald-300 px-3 py-1.5 text-xs font-bold text-emerald-700 hover:bg-emerald-50">
                    Mark resolved
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}

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
                    <th className="px-4 py-3 font-medium">Rating</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Veg?</th>
                    <th className="px-4 py-3 font-medium">Address</th>
                    <th className="px-4 py-3 font-medium">Website</th>
                    <th className="px-4 py-3 font-medium">Menu text</th>
                    <th className="px-4 py-3 font-medium">Vegan options</th>
                    <th className="px-4 py-3 font-medium">Actions</th>
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
                        {!r.is_consumer_venue && (
                          <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-800">
                            {r.consumer_hidden ? "hidden from Explore" : "non-restaurant"}
                          </span>
                        )}
                        {r.editorial_summary && (
                          <span className="ml-1 text-slate-300" title={r.editorial_summary}>
                            ⓘ
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <RatingBadge
                          rating={r.rating}
                          userRatingCount={r.user_rating_count}
                        />
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-col items-start gap-1">
                          <OpenStatusBadge openNow={r.open_now} enrichedAt={r.enriched_at} />
                          <FreshnessBadge fetchedAt={r.menu_fetched_at} compact />
                        </div>
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
                      <td className="px-4 py-3">
                        {r.dish_count > 0 ? (
                          <button
                            onClick={() => setDishesFor(r)}
                            className={`rounded px-2 py-0.5 text-xs font-medium hover:opacity-80 ${
                              r.vegan_options > 0
                                ? "bg-emerald-100 text-emerald-800"
                                : "bg-slate-100 text-slate-500"
                            }`}
                            title="View dishes and verdicts"
                          >
                            {r.vegan_options} of {r.dish_count}
                          </button>
                        ) : (
                          <span className="text-xs text-slate-300">—</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <div className="flex gap-1.5">
                          <button
                            onClick={() => toggleVisibility(r)}
                            disabled={!r.is_consumer_venue && !r.consumer_hidden}
                            title={
                              !r.is_consumer_venue && !r.consumer_hidden
                                ? "Automatically excluded by its Google place type"
                                : r.consumer_hidden
                                  ? "Restore this listing to Explore"
                                  : "Hide this listing from Explore"
                            }
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
                          >
                            {!r.is_consumer_venue && !r.consumer_hidden
                              ? "excluded"
                              : r.consumer_hidden
                                ? "show"
                                : "hide"}
                          </button>
                          <button
                            onClick={() => runRowAction(r, "ingest")}
                            disabled={rowBusy !== null || !r.website_url}
                            title={
                              r.website_url
                                ? "Re-run the menu scraper for this restaurant"
                                : "No website to scrape"
                            }
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {rowBusy?.id === r.id && rowBusy.action === "ingest"
                              ? "scraping…"
                              : "↻ rescrape"}
                          </button>
                          <button
                            onClick={() => runRowAction(r, "classify")}
                            disabled={rowBusy !== null || !r.has_menu_text}
                            title={
                              r.has_menu_text
                                ? `Re-run Claude dish classification (est ~$${(
                                    r.classify_estimate ?? 0.1
                                  ).toFixed(2)} for ${r.menu_chars?.toLocaleString() ?? "?"} chars of menu)`
                                : "No menu text to classify"
                            }
                            className="rounded border border-emerald-200 px-2 py-0.5 text-xs text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {rowBusy?.id === r.id && rowBusy.action === "classify"
                              ? "classifying…"
                              : "⚡ reclassify"}
                          </button>
                          {r.has_menu_text && (
                            <span
                              className="self-center whitespace-nowrap text-[10px] text-slate-400"
                              title={
                                r.last_classify_cost != null
                                  ? "Actual cost of the last classification run"
                                  : "Estimate from menu size — updates to the actual cost after a run"
                              }
                            >
                              {r.last_classify_cost != null
                                ? `$${r.last_classify_cost.toFixed(2)}`
                                : `~$${(r.classify_estimate ?? 0).toFixed(2)} est`}
                            </span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {addOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
          onClick={() => !adding && setAddOpen(false)}
        >
          <div
            className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-xl bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h2 className="font-semibold text-slate-900">Add restaurants by name</h2>
              <button
                onClick={() => !adding && setAddOpen(false)}
                className="text-slate-400 hover:text-slate-700"
              >
                ✕
              </button>
            </div>
            <div className="space-y-3 overflow-y-auto p-4">
              <p className="text-sm text-slate-500">
                One name per line. Each is resolved via Google Places, then
                enriched, scraped, and classified with Claude (~1–2 min and
                ~$0.10 of API credits each). Check the matched addresses
                below — a wrong match is worse than no match.
              </p>
              <textarea
                value={addNames}
                onChange={(e) => setAddNames(e.target.value)}
                rows={5}
                placeholder={"Ethos Vegan Kitchen\n4Rivers Smokehouse Winter Park"}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
              />
              <button
                onClick={submitAddNames}
                disabled={adding || !addNames.trim()}
                className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {adding ? "Adding (this can take a minute per restaurant)…" : "Add & scrape"}
              </button>
              {addResult?.error && (
                <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
                  {addResult.error}
                </div>
              )}
              {addResult?.matches?.length > 0 && (
                <ul className="space-y-1 text-sm">
                  {addResult.matches.map((m) => (
                    <li key={m.query} className="rounded bg-emerald-50 px-3 py-1.5">
                      <span className="font-medium text-emerald-800">{m.matched}</span>
                      <span className="ml-1 text-emerald-700/70">— {m.address}</span>
                    </li>
                  ))}
                </ul>
              )}
              {addResult?.not_found?.length > 0 && (
                <ul className="space-y-1 text-sm">
                  {addResult.not_found.map((n) => (
                    <li key={n} className="rounded bg-amber-50 px-3 py-1.5 text-amber-800">
                      not found: {n}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      {dishesFor && (
        <DishModal restaurant={dishesFor} onClose={() => setDishesFor(null)} />
      )}

      {menuFor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
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
