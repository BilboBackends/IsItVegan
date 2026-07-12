import { useEffect, useRef, useState } from "react";
import {
  CLOUD_ENABLED,
  GOOGLE_AUTH_ENABLED,
  onAuthChange,
  signInWithGoogle,
  signInWithMagicLink,
  signOut,
} from "./cloud.js";

// Header sign-in: Google one-click or an email magic link. Renders nothing
// when the cloud backend isn't configured, so the static site keeps its
// zero-account behavior until Supabase env vars exist at build time.
export default function AccountButton({ session, onSession }) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const popRef = useRef(null);

  useEffect(() => {
    if (!CLOUD_ENABLED) return undefined;
    let cleanup = () => {};
    onAuthChange((next) => onSession?.(next)).then((fn) => {
      cleanup = fn;
    });
    return () => cleanup();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event) => {
      if (popRef.current && !popRef.current.contains(event.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  if (!CLOUD_ENABLED) return null;

  const user = session?.user;
  const name =
    user?.user_metadata?.full_name ||
    (user?.email ? user.email.split("@")[0] : "account");

  async function sendLink(event) {
    event.preventDefault();
    if (!email.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await signInWithMagicLink(email.trim());
      setSent(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative" ref={popRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex h-9 items-center gap-2 rounded-full border px-3 text-sm font-semibold transition ${
          user
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-stone-200 bg-white text-stone-600 hover:border-emerald-300 hover:text-emerald-700"
        }`}
        title={user ? `Signed in as ${name}` : "Sign in to keep your saves and comment"}
      >
        {user ? (
          <>
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-700 text-xs font-bold uppercase text-white">
              {name.slice(0, 1)}
            </span>
            <span className="hidden max-w-[10rem] truncate sm:inline">{name}</span>
          </>
        ) : (
          "Sign in"
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-40 mt-2 w-72 rounded-xl border border-stone-200 bg-white p-4 shadow-lg">
          {user ? (
            <>
              <div className="text-sm font-semibold text-stone-800">{name}</div>
              {user.email && (
                <div className="mt-0.5 truncate text-xs text-stone-400">{user.email}</div>
              )}
              <p className="mt-2 text-xs text-stone-500">
                Your saves, likes, and comments follow this account on any device.
              </p>
              <button
                onClick={() => {
                  signOut();
                  setOpen(false);
                }}
                className="mt-3 w-full rounded-lg border border-stone-200 py-2 text-sm font-bold text-stone-600 hover:bg-stone-50"
              >
                Sign out
              </button>
            </>
          ) : sent ? (
            <div className="text-sm text-stone-600">
              <div className="font-semibold text-emerald-700">Check your email 📬</div>
              <p className="mt-1 text-xs">
                We sent a sign-in link to <span className="font-medium">{email}</span>.
                Open it on this device to finish signing in.
              </p>
            </div>
          ) : (
            <>
              <div className="text-sm font-bold text-stone-800">
                Keep your saves everywhere
              </div>
              <p className="mt-1 text-xs text-stone-500">
                Sign in to sync hearts and likes across devices and join
                restaurant discussions.
              </p>
              {GOOGLE_AUTH_ENABLED && (
                <>
                  <button
                    onClick={signInWithGoogle}
                    className="mt-3 flex w-full items-center justify-center gap-2 rounded-lg border border-stone-300 py-2 text-sm font-bold text-stone-700 hover:bg-stone-50"
                  >
                    <span aria-hidden>G</span> Continue with Google
                  </button>
                  <div className="my-3 flex items-center gap-2 text-[10px] font-bold uppercase tracking-wide text-stone-300">
                    <span className="h-px flex-1 bg-stone-200" /> or <span className="h-px flex-1 bg-stone-200" />
                  </div>
                </>
              )}
              <form onSubmit={sendLink} className="space-y-2">
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className="w-full rounded-lg border border-stone-300 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
                />
                <button
                  type="submit"
                  disabled={busy}
                  className="w-full rounded-lg bg-emerald-700 py-2 text-sm font-bold text-white hover:bg-emerald-800 disabled:bg-stone-300"
                >
                  {busy ? "Sending…" : "Email me a sign-in link"}
                </button>
              </form>
              {error && <div className="mt-2 text-xs text-rose-600">{error}</div>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
