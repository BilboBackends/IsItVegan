# Accounts / comments backend — one-time Supabase setup

The public site stays a static GitHub Pages build. Accounts, persistent
favorites/likes, and restaurant comment threads live in a free Supabase
project the browser talks to directly. Until the env vars below are set,
every account feature is invisible and the site behaves exactly as before.

## 1. Create the project (~5 min)

1. https://supabase.com → New project (free tier). Pick a strong DB password
   (you won't need it day-to-day) and a region near Orlando. Under Security,
   enable the Data API, disable automatic exposure of new tables, and enable
   automatic RLS. `schema.sql` contains the explicit least-privilege grants.
2. SQL Editor → paste ALL of `supabase/schema.sql` → Run. This creates the
   tables and, critically, the Row Level Security policies — the anon key is
   safe to ship precisely because these policies are what authorize writes.

## 2. Enable sign-in methods

Authentication → Providers:
- **Email**: ON, and turn OFF "Confirm email" double-opt-in if you want magic
  links to sign users straight in (magic link itself proves the address).
- **Google**: ON. Needs a Google Cloud OAuth client (free):
  console.cloud.google.com → APIs & Services → Credentials → Create OAuth
  client ID (Web). Authorized redirect URI = the callback URL Supabase shows
  on the Google provider screen. Paste client id + secret back into Supabase.

Authentication → URL Configuration:
- Site URL: `https://dishtune.com/` (the apex is the canonical domain; www
  redirects to it).
- Additional redirect URLs: `http://localhost:5173/` (local dev) and, if the
  github.io URL should keep working, `https://<username>.github.io/IsItVegan/`.

## 3. Wire the frontend

Project Settings → API: copy the **Project URL** and the **publishable** key
(NOT the secret or service_role key — those never enter the frontend).

Local dev — `frontend/.env.local` (gitignored):

    VITE_SUPABASE_URL=https://xxxx.supabase.co
    VITE_SUPABASE_ANON_KEY=eyJ...
    # Add only after the Google provider is configured:
    VITE_SUPABASE_GOOGLE_ENABLED=true

Published site — the values are compile-time, so add them wherever the
static build runs (publish_static.py environment or the shell):

    set VITE_SUPABASE_URL=... && set VITE_SUPABASE_ANON_KEY=... (Windows)

The anon key is designed to be public (it's in every visitor's browser
anyway); RLS is the security boundary. Keep service_role local-only.

## 4. Moderation

Comment inserts require a signed-in user and are rate-limited to 10/hour by
a DB trigger. Users can delete their own comments and report others'.
Reviewing reports / deleting abuse: Supabase Dashboard → Table editor →
comments / comment_reports (or wire the local Admin to the service key
later).

## 5. Optional usernames and @mentions

Signed-in users may choose a unique username from the account menu. It is
shown publicly with their notes and lets other users mention them with
`@username`, but it is not used to sign in. Leaving it blank keeps their
Google/email identity private and shows `Dish Explorer` on public notes
instead.

Replies do not require a username. Each reply stores the original note's
`parent_comment_id`, so a user displayed as `Dish Explorer` can still take
part in a directed conversation. Apply migrations before publishing frontend
changes:

    npx supabase db push

## What lives where

| Data                          | Home                                    |
|-------------------------------|-----------------------------------------|
| Menus, dishes, verdicts       | veganfind.db → static JSON (unchanged)  |
| Accounts, favorites, votes, comments | Supabase (this project)          |

Rows key on restaurant `place_id` + normalized dish name — NOT numeric dish
ids, which renumber on full reclassification. `local_id` is only a hint.
