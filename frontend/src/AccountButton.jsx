import { useEffect, useRef, useState } from "react";
import {
  CLOUD_ENABLED,
  GOOGLE_AUTH_ENABLED,
  fetchProfile,
  onAuthChange,
  signInWithMagicLink,
  signOut,
  updateUsername,
} from "./cloud.js";
import GoogleSignInButton from "./GoogleSignInButton.jsx";
import {
  DEFAULT_PUBLIC_NAME,
  USERNAME_MAX_LENGTH,
  accountFallbackName,
  normalizeUsername,
  validateUsername,
} from "./username.js";

// Header sign-in: Google one-click or an email magic link. Renders nothing
// when the cloud backend isn't configured, so the static site keeps its
// zero-account behavior until Supabase env vars exist at build time.
export default function AccountButton({
  session,
  profile,
  onSession,
  onProfile,
}) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [usernameDraft, setUsernameDraft] = useState("");
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState(null);
  const [profileSaved, setProfileSaved] = useState(false);
  const popRef = useRef(null);
  const triggerRef = useRef(null);
  const user = session?.user;

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
    const closeOnEscape = (event) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  useEffect(() => {
    if (!user?.id) {
      onProfile?.(null);
      setUsernameDraft("");
      return undefined;
    }
    let cancelled = false;
    setProfileError(null);
    fetchProfile(user.id)
      .then((row) => {
        if (cancelled) return;
        onProfile?.(row);
        setUsernameDraft(row?.username || "");
      })
      .catch((profileLoadError) => {
        if (!cancelled) setProfileError(profileLoadError.message);
      });
    return () => {
      cancelled = true;
    };
  }, [user?.id, onProfile]);

  useEffect(() => {
    setUsernameDraft(profile?.username || "");
  }, [profile?.username]);

  if (!CLOUD_ENABLED) return null;

  const privateName = accountFallbackName(user);
  const name = profile?.username ? `@${profile.username}` : privateName;
  const avatarLetter = (profile?.username || privateName).slice(0, 1);
  const normalizedDraft = normalizeUsername(usernameDraft);
  const usernameChanged = normalizedDraft !== (profile?.username || "");

  async function persistUsername(value) {
    if (!user?.id || profileBusy) return;
    const validation = validateUsername(value);
    if (validation.error) {
      setProfileError(validation.error);
      setProfileSaved(false);
      return;
    }
    setProfileBusy(true);
    setProfileError(null);
    setProfileSaved(false);
    try {
      const saved = await updateUsername(user.id, validation.value);
      onProfile?.(saved);
      setUsernameDraft(saved?.username || "");
      setProfileSaved(true);
    } catch (profileSaveError) {
      setProfileError(profileSaveError.message);
    } finally {
      setProfileBusy(false);
    }
  }

  function saveUsername(event) {
    event.preventDefault();
    persistUsername(usernameDraft);
  }

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
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={user ? `Account menu for ${name}` : "Sign in"}
        className={`flex h-9 items-center gap-2 rounded-full border px-3 text-sm font-semibold transition ${
          user
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-stone-200 bg-white text-stone-600 hover:border-emerald-300 hover:text-emerald-700"
        }`}
        title={
          user
            ? `Signed in as ${name}`
            : "Sign in to keep your saves and add notes"
        }
      >
        {user ? (
          <>
            <span
              aria-hidden="true"
              className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-700 text-xs font-bold uppercase text-white"
            >
              {avatarLetter}
            </span>
            <span className="hidden max-w-[10rem] truncate sm:inline">{name}</span>
          </>
        ) : (
          "Sign in"
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label={user ? "Account settings" : "Sign in"}
          className="absolute right-0 z-40 mt-2 w-80 max-w-[calc(100vw-2rem)] rounded-xl border border-stone-200 bg-white p-4 shadow-lg"
        >
          {user ? (
            <>
              <div className="text-sm font-semibold text-stone-800">{name}</div>
              {user.email && (
                <div className="mt-0.5 truncate text-xs text-stone-400">{user.email}</div>
              )}
              <p className="mt-2 text-xs text-stone-500">
                Your saves, likes, and notes follow this account on any device.
              </p>
              <form
                onSubmit={saveUsername}
                className="mt-3 border-t border-stone-100 pt-3"
              >
                <div className="flex items-baseline justify-between gap-3">
                  <label
                    htmlFor="account-username"
                    className="text-xs font-bold text-stone-700"
                  >
                    Username{" "}
                    <span className="font-normal text-stone-400">
                      (optional)
                    </span>
                  </label>
                  <span className="text-[10px] tabular-nums text-stone-400">
                    {usernameDraft.length}/{USERNAME_MAX_LENGTH}
                  </span>
                </div>
                <div className="relative mt-1.5">
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm font-bold text-stone-400"
                  >
                    @
                  </span>
                  <input
                    id="account-username"
                    type="text"
                    autoComplete="nickname"
                    autoCapitalize="none"
                    spellCheck="false"
                    maxLength={USERNAME_MAX_LENGTH}
                    pattern="[A-Za-z0-9][A-Za-z0-9_]{2,19}"
                    title="3–20 letters, numbers, or underscores; start with a letter or number"
                    value={usernameDraft}
                    onChange={(event) => {
                      setUsernameDraft(event.target.value);
                      setProfileError(null);
                      setProfileSaved(false);
                    }}
                    placeholder="veganfriend"
                    aria-describedby="account-username-help"
                    className="w-full rounded-lg border border-stone-300 py-2 pl-7 pr-3 text-sm outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
                  />
                </div>
                <p
                  id="account-username-help"
                  className="mt-1.5 text-[11px] leading-relaxed text-stone-500"
                >
                  Your unique public name on notes. Other people can mention
                  you with <span className="font-semibold">@username</span>.
                  Without one, your notes show {DEFAULT_PUBLIC_NAME}. It is not
                  used to sign in.
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="submit"
                    disabled={profileBusy || !usernameChanged}
                    className="rounded-lg bg-emerald-700 px-3 py-1.5 text-xs font-bold text-white hover:bg-emerald-800 disabled:bg-stone-300"
                  >
                    {profileBusy ? "Saving…" : "Save username"}
                  </button>
                  {profile?.username && (
                    <button
                      type="button"
                      disabled={profileBusy}
                      onClick={() => persistUsername(null)}
                      className="px-2 py-1.5 text-xs font-semibold text-stone-400 hover:text-rose-600 disabled:text-stone-300"
                    >
                      Remove
                    </button>
                  )}
                </div>
                {profileSaved && (
                  <div
                    role="status"
                    className="mt-2 text-xs font-semibold text-emerald-700"
                  >
                    Username saved.
                  </div>
                )}
                {profileError && (
                  <div role="alert" className="mt-2 text-xs text-rose-600">
                    {profileError}
                  </div>
                )}
              </form>
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
              <div className="font-semibold text-emerald-700">
                Check your email 📬
              </div>
              <p className="mt-1 text-xs">
                We sent a sign-in link to{" "}
                <span className="font-medium">{email}</span>. Open it on this
                device to finish signing in.
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
                  <GoogleSignInButton
                    onError={(googleError) => setError(googleError.message)}
                    className="mt-3 flex justify-center"
                  />
                  <div className="my-3 flex items-center gap-2 text-[10px] font-bold uppercase tracking-wide text-stone-300">
                    <span className="h-px flex-1 bg-stone-200" /> or{" "}
                    <span className="h-px flex-1 bg-stone-200" />
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
