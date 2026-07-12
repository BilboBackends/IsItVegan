import { useEffect, useState } from "react";
import { STATIC_MODE } from "./staticData.js";
import { ProfileContext, SessionContext } from "./cloud.js";
import AccountButton from "./AccountButton.jsx";
import ExploreHub from "./ExploreHub.jsx";
import Admin from "./Admin.jsx";

// Shell: hash-routed views. Consumers can browse restaurants or search the
// cross-menu dish index; #admin holds discovery/ingest/enrich controls.

export default function App() {
  const [hash, setHash] = useState(window.location.hash);
  // Supabase session when the account backend is configured; null otherwise.
  const [session, setSession] = useState(null);
  const [profile, setProfile] = useState(null);

  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // The static public build is a consumer product. Keep the local pipeline
  // dashboard out of both its navigation and its hash-routed surface.
  const isAdmin = !STATIC_MODE && hash.startsWith("#admin");
  const exploreView = hash.startsWith("#dishes")
    ? "food"
    : hash.startsWith("#saved")
      ? "saved"
      : "restaurants";

  return (
    <SessionContext.Provider value={session}>
      <ProfileContext.Provider value={profile}>
        <div className="min-h-screen bg-[#faf8f4] text-stone-900">
          {/* z-30: the account dropdown lives inside this stacking context, so
              the nav must sit above the z-20 sticky tab bar (equal z loses by
              DOM order) while staying under the z-50+ modal overlays. */}
          <nav className="sticky top-0 z-30 border-b border-stone-200/80 bg-[#faf8f4]/90 backdrop-blur">
            <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3">
              <a
                href="#"
                className="flex items-center gap-2 text-lg font-extrabold tracking-tight text-emerald-800"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-700 text-base text-white">
                  🌱
                </span>
                <span>DishTune</span>
              </a>
              <div className="flex items-center gap-2">
                <AccountButton
                  session={session}
                  profile={profile}
                  onSession={(next) => {
                    setSession(next);
                    if (!next) setProfile(null);
                  }}
                  onProfile={setProfile}
                />
                {!STATIC_MODE && (
                  <div className="flex gap-1 rounded-full border border-stone-200 bg-white p-1 text-xs shadow-sm sm:text-sm">
                    <a
                      href="#restaurants"
                      className={`rounded-full px-2.5 py-1.5 font-semibold transition sm:px-4 ${
                        !isAdmin
                          ? "bg-emerald-700 text-white"
                          : "text-stone-500 hover:text-stone-800"
                      }`}
                    >
                      Explore
                    </a>
                    <a
                      href="#admin"
                      className={`rounded-full px-2.5 py-1.5 font-semibold transition sm:px-4 ${
                        isAdmin
                          ? "bg-emerald-700 text-white"
                          : "text-stone-500 hover:text-stone-800"
                      }`}
                    >
                      Admin
                    </a>
                  </div>
                )}
              </div>
            </div>
          </nav>
          {isAdmin ? <Admin /> : <ExploreHub view={exploreView} />}
        </div>
      </ProfileContext.Provider>
    </SessionContext.Provider>
  );
}
