import { useEffect, useState } from "react";
import Explore from "./Explore.jsx";
import Admin from "./Admin.jsx";

// Shell: hash-routed views. Default is the consumer-facing Explore view;
// #admin is the pipeline dashboard (discovery/ingest/enrich controls).

export default function App() {
  const [hash, setHash] = useState(window.location.hash);

  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const isAdmin = hash === "#admin";

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <nav className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <a href="#" className="text-lg font-bold text-emerald-700">
            🌱 VeganFind
          </a>
          <div className="flex gap-1 text-sm">
            <a
              href="#"
              className={`rounded-lg px-3 py-1.5 font-medium ${
                !isAdmin
                  ? "bg-emerald-50 text-emerald-700"
                  : "text-slate-500 hover:text-slate-800"
              }`}
            >
              Explore
            </a>
            <a
              href="#admin"
              className={`rounded-lg px-3 py-1.5 font-medium ${
                isAdmin
                  ? "bg-emerald-50 text-emerald-700"
                  : "text-slate-500 hover:text-slate-800"
              }`}
            >
              Admin
            </a>
          </div>
        </div>
      </nav>
      {isAdmin ? <Admin /> : <Explore />}
    </div>
  );
}
