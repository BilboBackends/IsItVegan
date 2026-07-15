import { useEffect, useState } from "react";
import { STATIC_MODE } from "./staticData.js";
import { ProfileContext, SessionContext } from "./cloud.js";
import AccountButton from "./AccountButton.jsx";
import ExploreHub from "./ExploreHub.jsx";
import Admin from "./Admin.jsx";
import AdminActivity from "./AdminActivity.jsx";
import { replaceHashRouteFromClick } from "./hashNavigation.js";

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
    window.addEventListener("popstate", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("popstate", onHash);
    };
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
                href="#restaurants"
                onClick={(event) =>
                  replaceHashRouteFromClick(event, "restaurants")
                }
                className="flex items-center gap-2 text-lg font-extrabold tracking-tight text-emerald-800"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-700 text-base text-white">
                  🌱
                </span>
                <span>DishTune</span>
              </a>
              <div className="flex items-center gap-2">
                {/* Legal links stay visible in the header — the page footer
                    sits below hundreds of cards where nobody finds it. Phones
                    have no header room; they keep the footer links. */}
                {/* .html works everywhere: Vite dev serves public/ files only
                    at their literal names; Cloudflare redirects to the clean
                    /privacy and /terms URLs in production. */}
                <div className="mr-1 hidden items-center gap-2 text-xs text-stone-400 sm:flex">
                  <a href="/privacy.html" className="hover:text-stone-600 hover:underline">
                    Privacy
                  </a>
                  <span aria-hidden="true">·</span>
                  <a href="/terms.html" className="hover:text-stone-600 hover:underline">
                    Terms
                  </a>
                </div>
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
                      onClick={(event) =>
                        replaceHashRouteFromClick(event, "restaurants")
                      }
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
                      onClick={(event) =>
                        replaceHashRouteFromClick(event, "admin")
                      }
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
          {isAdmin ? (
            hash.startsWith("#admin/activity") ? (
              <AdminActivity />
            ) : (
              <Admin />
            )
          ) : (
            <>
              <ExploreHub view={exploreView} />
              {/* Google OAuth brand verification checks that the homepage
                  links the published policies. */}
              <footer className="pb-8 pt-2 text-center text-xs text-stone-400">
                <a href="/privacy.html" className="hover:text-stone-600 hover:underline">
                  Privacy
                </a>
                {" · "}
                <a href="/terms.html" className="hover:text-stone-600 hover:underline">
                  Terms
                </a>
              </footer>
            </>
          )}
        </div>
      </ProfileContext.Provider>
    </SessionContext.Provider>
  );
}
