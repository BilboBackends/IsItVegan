---
name: run-app
description: Launch the DishTune frontend dev server and drive it in a headless browser (screenshots, click-throughs) to verify a change in the real app. Use when asked to run, start, screenshot, or visually verify the frontend.
---

# Run the DishTune frontend

## Dev server

Vite serves the consumer app on **port 5173** (falls back to 5174+ if taken —
don't accept that: a fallback port usually means a stale server from an
earlier session is still holding 5173, and it won't have current `.env.local`
values, since Vite reads env files only at startup).

```powershell
# Kill anything stale on 5173 first (it's always a node/Vite leftover):
Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }

# Launch (run_in_background):
Set-Location frontend; npm run dev
```

Then poll — don't sleep (Bash tool):

```bash
timeout 30 bash -c 'until curl -sf http://localhost:5173 >/dev/null; do sleep 1; done'
```

Env lives in `frontend/.env.local` (gitignored): Supabase URL + anon key,
`VITE_SUPABASE_GOOGLE_ENABLED`, `VITE_GOOGLE_CLIENT_ID`. Cloud features
(sign-in, comments, synced hearts) silently disappear from the UI when these
are absent — that's the feature flag working, not a bug. To confirm an env
var was picked up, grep the served module, e.g.
`curl -s http://localhost:5173/src/cloud.js | grep -c <value>`.

The dev server serves live data snapshots from `frontend/public/data/` and
hot-reloads when the pipeline republishes them — a burst of `page reload`
lines in its output is normal.

## Drive it headlessly

`chromium-cli` and project-local Playwright are NOT available. The proven
recipe: **puppeteer-core in the scratchpad + system Chrome** (nothing gets
added to the project):

```bash
cd <scratchpad> && npm init -y && npm i puppeteer-core --no-audit --no-fund
```

Driver skeleton (`.mjs`, run with `node`):

```js
import puppeteer from "puppeteer-core";

const browser = await puppeteer.launch({
  executablePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  headless: "new",
});
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900 });
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
await page.goto("http://localhost:5173/", { waitUntil: "networkidle2", timeout: 60000 });
// ... interact ...
await page.screenshot({ path: "shot.png" });
console.log(errors);
await browser.close();
```

**Look at the screenshot** with Read; a blank frame means the app didn't
render. Always print console errors before declaring success.

## App landmarks for interaction

- Header account popover: `button[aria-label="Sign in"]` (signed-out) or
  `button[aria-label^="Account menu"]` (signed-in). Popover copy when open:
  "Keep your saves everywhere".
- Google sign-in button is a GIS iframe: assert
  `iframe[src*="accounts.google.com"]` exists; it renders only when both
  Google env vars are set. A `[GSI_LOGGER]: The given origin is not allowed`
  console error means the current origin is missing from the OAuth client's
  Authorized JavaScript origins in Google Cloud console — the button still
  renders but clicking fails.
- Restaurant cards render on the default Restaurants tab; comments open via
  the note-count chip on a card, and the signed-out comments box copy is
  "Join the conversation".
- A single 404 console error on page load is pre-existing background noise,
  not a regression signal by itself.

## Full sign-in cannot be automated

Completing Google sign-in needs a real Google account interaction; magic
links need an inbox. Verify up to the rendered control/popup, then hand off
to the user for the credentialed step.
