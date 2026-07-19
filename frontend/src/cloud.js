// The user-data plane: accounts, persistent favorites/votes, and restaurant
// comment threads, backed by Supabase (see supabase/SETUP.md). The static
// site has no backend of ours, so the browser talks to Supabase directly
// with the publishable anon key; Row Level Security is the entire write
// authorization story. Feature-flagged: without the two env vars every
// export is inert and the site behaves exactly as before.
//
// Content identity: numeric dish/restaurant ids renumber when the pipeline
// fully reclassifies a menu, so cloud rows key on the restaurant's Google
// place_id plus a normalized dish name (dishKey). Numeric ids ride along
// as local_id — a resolution hint so favorites restore instantly even
// before the datasets load.

import { createContext } from "react";
import { attachReplyTargets } from "./commentReplies.js";
import { mergeNotificationEvents } from "./notifications.js";

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL;
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const CLOUD_ENABLED = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);
const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID;
export const GOOGLE_AUTH_ENABLED =
  import.meta.env.VITE_SUPABASE_GOOGLE_ENABLED === "true" &&
  Boolean(GOOGLE_CLIENT_ID);

const COMMENT_AUTH_RETURN_KEY = "dishtune:commentAuthReturn";
const COMMENT_AUTH_RETURN_MAX_AGE = 60 * 60 * 1000;

export function rememberCommentAuthReturn(placeId, dishName = null) {
  if (!placeId) return;
  try {
    window.localStorage.setItem(
      COMMENT_AUTH_RETURN_KEY,
      JSON.stringify({ placeId, dishName, createdAt: Date.now() })
    );
  } catch {
    // The query-string fallback still works when storage is unavailable.
  }
}

export function pendingCommentAuthReturn() {
  try {
    const value = JSON.parse(
      window.localStorage.getItem(COMMENT_AUTH_RETURN_KEY) || "null"
    );
    if (
      !value?.placeId ||
      !value?.createdAt ||
      Date.now() - value.createdAt > COMMENT_AUTH_RETURN_MAX_AGE
    ) {
      window.localStorage.removeItem(COMMENT_AUTH_RETURN_KEY);
      return null;
    }
    return { placeId: value.placeId, dishName: value.dishName || null };
  } catch {
    return null;
  }
}

export function clearCommentAuthReturn() {
  try {
    window.localStorage.removeItem(COMMENT_AUTH_RETURN_KEY);
  } catch {
    // Nothing else to clear when storage is unavailable.
  }
}

// The signed-in Supabase session (or null), provided from App so deep
// components (thumbs, comments) don't need prop-drilling.
export const SessionContext = createContext(null);
export const ProfileContext = createContext(null);

let clientPromise = null;

// The supabase-js chunk (~30 kB gz) loads only when the feature is on.
function getClient() {
  if (!CLOUD_ENABLED) return Promise.resolve(null);
  if (!clientPromise) {
    clientPromise = import("@supabase/supabase-js").then(({ createClient }) =>
      createClient(SUPABASE_URL, SUPABASE_ANON_KEY)
    );
  }
  return clientPromise;
}

// ---------------------------------------------------------------- identity

// Mirrors the spirit of the pipeline's dish_identity_key: case, whitespace,
// and punctuation noise must not fork a dish's cloud identity.
export function dishKey(name) {
  return (name || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim()
    .replace(/\s+/g, "-")
    .slice(0, 200);
}

// Registry: content datasets announce themselves as they load, so cloud
// writes can attach stable keys and cloud reads can resolve back to the
// numeric ids the UI components use. Sparse by design — an unresolvable
// row degrades to its local_id hint, never an error.
const registry = {
  dishesById: new Map(),       // dish_id -> dish row
  restaurantsById: new Map(),  // restaurant_id -> restaurant row
  placeIdByRestaurantId: new Map(),
  restaurantIdByPlaceId: new Map(),
};

export function registerDishes(dishes) {
  for (const dish of dishes || []) registry.dishesById.set(dish.id, dish);
}

export function registerRestaurants(restaurants) {
  for (const r of restaurants || []) {
    registry.restaurantsById.set(r.id, r);
    if (r.place_id) {
      registry.placeIdByRestaurantId.set(r.id, r.place_id);
      registry.restaurantIdByPlaceId.set(r.place_id, r.id);
    }
  }
}

// Display name for a place_id once the restaurant datasets have announced
// themselves; null (caller shows a generic label) before that.
export function restaurantNameForPlaceId(placeId) {
  const restaurantId = registry.restaurantIdByPlaceId.get(placeId);
  if (restaurantId == null) return null;
  return registry.restaurantsById.get(restaurantId)?.name || null;
}

// Stable identity for a favorite/vote target, or null when the datasets
// needed to build it haven't loaded yet.
function identify(kind, id) {
  if (kind === "restaurants" || kind === "restaurant") {
    const placeId = registry.placeIdByRestaurantId.get(id);
    return placeId
      ? { kind: "restaurant", place_id: placeId, dish_key: "", dish_name: null }
      : null;
  }
  const dish = registry.dishesById.get(id);
  if (!dish) return null;
  const placeId = registry.placeIdByRestaurantId.get(dish.restaurant_id);
  if (!placeId) return null;
  return {
    kind: "dish",
    place_id: placeId,
    dish_key: dishKey(dish.name),
    dish_name: dish.name,
  };
}

// -------------------------------------------------------------------- auth

export async function onAuthChange(callback) {
  const client = await getClient();
  if (!client) return () => {};
  const { data } = await client.auth.getSession();
  callback(data?.session ?? null);
  const { data: sub } = client.auth.onAuthStateChange((_event, session) => {
    callback(session ?? null);
  });
  return () => sub?.subscription?.unsubscribe();
}

function authRedirectUrl(returnTo) {
  const fallback = new URL(window.location.pathname, window.location.origin);
  if (!returnTo) return fallback.toString();
  const candidate = new URL(returnTo, window.location.origin);
  return candidate.origin === window.location.origin
    ? candidate.toString()
    : fallback.toString();
}

// Google sign-in goes through Google Identity Services + signInWithIdToken
// rather than signInWithOAuth: the OAuth redirect flow's redirect URI lives
// on <ref>.supabase.co, so Google's consent popup names the Supabase domain
// instead of dishtune.com. GIS runs against our own OAuth client (origin =
// dishtune.com) and hands back an ID token that Supabase exchanges for the
// same session — no redirect, no page reload.

let gsiScriptPromise = null;

function loadGoogleIdentity() {
  if (window.google?.accounts?.id) return Promise.resolve(window.google);
  if (!gsiScriptPromise) {
    gsiScriptPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://accounts.google.com/gsi/client";
      script.async = true;
      script.onload = () => resolve(window.google);
      script.onerror = () => {
        gsiScriptPromise = null; // ad blockers eat this script; allow retry
        script.remove();
        reject(new Error("Could not load Google sign-in."));
      };
      document.head.appendChild(script);
    });
  }
  return gsiScriptPromise;
}

// Replay protection: GIS receives the SHA-256 of a fresh nonce (Google bakes
// it into the ID token), and Supabase receives the raw nonce to hash and
// compare against that claim.
async function generateNonce() {
  const raw = crypto.randomUUID();
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(raw)
  );
  const hashed = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return { raw, hashed };
}

// Renders Google's official sign-in button into `container` (the ID-token
// flow cannot be triggered from a custom button). Success needs no callback
// here: signInWithIdToken fires onAuthStateChange and the UI re-renders
// signed-in in place, so magic-link-style return URLs don't apply.
export async function renderGoogleSignInButton(container, { onError } = {}) {
  if (!GOOGLE_AUTH_ENABLED || !container) return false;
  const [client, google, nonce] = await Promise.all([
    getClient(),
    loadGoogleIdentity(),
    generateNonce(),
  ]);
  if (!client || !google || !container.isConnected) return false;
  // initialize() is global to the page: the newest call's nonce + callback
  // pair governs every rendered button, so the two stay consistent even
  // with both sign-in surfaces (header popover, comments box) mounted.
  google.accounts.id.initialize({
    client_id: GOOGLE_CLIENT_ID,
    nonce: nonce.hashed,
    callback: async (response) => {
      const { error } = await client.auth.signInWithIdToken({
        provider: "google",
        token: response.credential,
        nonce: nonce.raw,
      });
      if (error) onError?.(new Error(error.message));
    },
  });
  container.replaceChildren();
  google.accounts.id.renderButton(container, {
    type: "standard",
    theme: "outline",
    size: "large",
    text: "continue_with",
    width: Math.min(container.offsetWidth || 320, 400),
  });
  return true;
}

export async function signInWithMagicLink(email, returnTo) {
  const client = await getClient();
  const { error } = await client.auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: authRedirectUrl(returnTo),
    },
  });
  if (error) throw new Error(error.message);
}

export async function signOut() {
  const client = await getClient();
  await client?.auth.signOut();
}

// --------------------------------------------------------------- profiles

export async function fetchProfile(userId) {
  if (!userId) return null;
  const client = await getClient();
  if (!client) return null;
  const { data, error } = await client
    .from("profiles")
    .select("id, username")
    .eq("id", userId)
    .maybeSingle();
  if (error) throw new Error(error.message);
  return data || null;
}

export async function searchUsernames(query = "") {
  if (!CLOUD_ENABLED) return [];
  const client = await getClient();
  const prefix = String(query || "").toLowerCase();
  let request = client
    .from("profiles")
    .select("id, username")
    .not("username", "is", null)
    .order("username");
  if (prefix) {
    request = request.gte("username", prefix).lt("username", `${prefix}\uffff`);
  }
  const { data, error } = await request.limit(6);
  if (error) throw new Error(error.message);
  return data || [];
}

export async function updateUsername(userId, username) {
  const client = await getClient();
  if (!client || !userId) throw new Error("Sign in before changing your username.");
  const { data, error } = await client
    .from("profiles")
    .update({ username: username || null })
    .eq("id", userId)
    .select("id, username")
    .single();
  if (error) {
    if (error.code === "23505" || /duplicate|unique/i.test(error.message)) {
      throw new Error("That username is already taken.");
    }
    if (error.code === "23514") {
      throw new Error("That username is not allowed.");
    }
    throw new Error(error.message);
  }
  window.dispatchEvent(
    new CustomEvent("dishtune:profile-changed", { detail: data })
  );
  return data;
}

// --------------------------------------------------------------- favorites

// Pull the account's favorites as {dishes: [ids], restaurants: [ids]},
// preferring registry resolution (survives id renumbering) and falling back
// to the stored local_id hint.
export async function pullFavorites({ beforeResolveDishes } = {}) {
  const client = await getClient();
  if (!client) return null;
  const { data, error } = await client.from("favorites").select("*");
  if (error || !data) return null;
  if (beforeResolveDishes) {
    const restaurantIds = [
      ...new Set(
        data
          .filter((row) => row.kind === "dish")
          .map((row) => registry.restaurantIdByPlaceId.get(row.place_id))
          .filter((id) => id != null)
      ),
    ];
    if (restaurantIds.length > 0) await beforeResolveDishes(restaurantIds);
  }
  const out = { dishes: [], restaurants: [] };
  for (const row of data) {
    if (row.kind === "restaurant") {
      const id =
        registry.restaurantIdByPlaceId.get(row.place_id) ?? row.local_id;
      if (id != null) out.restaurants.push(Number(id));
    } else {
      let id = row.local_id;
      const restaurantId = registry.restaurantIdByPlaceId.get(row.place_id);
      if (restaurantId != null) {
        for (const dish of registry.dishesById.values()) {
          if (
            dish.restaurant_id === restaurantId &&
            dishKey(dish.name) === row.dish_key
          ) {
            id = dish.id;
            break;
          }
        }
      }
      if (id != null) out.dishes.push(Number(id));
    }
  }
  return out;
}

// Write-through for one heart toggle. Unresolvable targets (datasets not
// loaded yet) sync as a bare local_id hint so nothing is ever dropped.
export async function syncFavorite(kind, id, active, userId) {
  const client = await getClient();
  if (!client || !userId) return;
  const identity = identify(kind, id) ?? {
    kind: kind === "restaurants" ? "restaurant" : "dish",
    place_id: `local:${id}`,
    dish_key: "",
    dish_name: null,
  };
  if (active) {
    await client.from("favorites").upsert(
      { user_id: userId, ...identity, local_id: id },
      { onConflict: "user_id,kind,place_id,dish_key" }
    );
  } else {
    await client
      .from("favorites")
      .delete()
      .match({
        user_id: userId,
        kind: identity.kind,
        place_id: identity.place_id,
        dish_key: identity.dish_key,
      });
  }
}

// First sign-in on this browser: nothing saved anonymously is lost.
export async function mergeLocalFavorites(local, userId) {
  const client = await getClient();
  if (!client || !userId || !local) return;
  const rows = [];
  for (const id of local.restaurants || []) {
    const identity = identify("restaurants", id);
    if (identity) rows.push({ user_id: userId, ...identity, local_id: id });
  }
  for (const id of local.dishes || []) {
    const identity = identify("dishes", id);
    if (identity) rows.push({ user_id: userId, ...identity, local_id: id });
  }
  if (rows.length) {
    await client
      .from("favorites")
      .upsert(rows, { onConflict: "user_id,kind,place_id,dish_key" });
  }
}

// ------------------------------------------------------------------- votes

export async function syncVote(kind, id, vote, userId) {
  const client = await getClient();
  if (!client || !userId) return;
  const identity = identify(kind, id);
  if (!identity) return; // datasets not loaded; anonymous path still records
  if (vote) {
    await client.from("votes").upsert(
      {
        user_id: userId,
        ...identity,
        vote,
        local_id: id,
        updated_at: new Date().toISOString(),
      },
      { onConflict: "user_id,kind,place_id,dish_key" }
    );
  } else {
    await client
      .from("votes")
      .delete()
      .match({
        user_id: userId,
        kind: identity.kind,
        place_id: identity.place_id,
        dish_key: identity.dish_key,
      });
  }
}

// ---------------------------------------------------------------- comments

// Usernames are stitched in with a second query instead of a PostgREST
// relationship embed: duplicate FKs (schema.sql + migrations both applied)
// make embeds ambiguous ("more than one relationship was found"), and the
// stitch works regardless of the database's constraint history.
async function attachUsernames(client, comments) {
  const userIds = [
    ...new Set(
      comments.flatMap((comment) => [
        comment.user_id,
        ...(comment.user_mentions || []).map((mention) => mention?.user_id),
      ])
    ),
  ].filter(Boolean);
  if (userIds.length === 0) return comments;
  const names = new Map();
  // A thread can contain hundreds of distinct mentions. Keep each PostgREST
  // URL bounded instead of placing every UUID in one very long `in` filter.
  const chunks = [];
  for (let index = 0; index < userIds.length; index += 100) {
    chunks.push(userIds.slice(index, index + 100));
  }
  const responses = await Promise.all(
    chunks.map((ids) =>
      client.from("profiles").select("id, username").in("id", ids)
    )
  );
  for (const { data } of responses) {
    for (const row of data || []) names.set(row.id, row.username);
  }
  return comments.map((c) => ({
    ...c,
    profiles: { username: names.get(c.user_id) || null },
    user_mentions: (c.user_mentions || []).map((mention) => ({
      ...mention,
      current_username: names.get(mention?.user_id) || null,
    })),
  }));
}

export async function fetchComments(placeId) {
  const client = await getClient();
  if (!client) return [];
  const { data, error } = await client
    .from("comments")
    .select(
      "id, user_id, body, mentions, user_mentions, parent_comment_id, created_at"
    )
    .eq("place_id", placeId)
    .order("created_at", { ascending: false })
    .limit(100);
  if (error) throw new Error(error.message);
  const comments = await attachUsernames(client, data || []);
  const commentIds = new Set(comments.map((comment) => comment.id));
  const missingParentIds = [
    ...new Set(
      comments
        .map((comment) => comment.parent_comment_id)
        .filter((id) => id && !commentIds.has(id))
    ),
  ];
  let extraParents = [];
  if (missingParentIds.length > 0) {
    const { data: parentRows, error: parentError } = await client
      .from("comments")
      .select("id, user_id, body, mentions, created_at")
      .eq("place_id", placeId)
      .in("id", missingParentIds);
    if (parentError) {
      console.warn("Could not load reply context:", parentError.message);
    } else {
      extraParents = await attachUsernames(client, parentRows || []);
    }
  }
  return attachReplyTargets(comments, extraParents);
}

// place_id -> comment count, for the note chips on restaurant cards. Counting
// client-side over a bare place_id column is fine at MVP scale (PostgREST
// caps the response at 1000 rows); switch to a count() aggregate or a view
// when threads outgrow that.
let commentCountsCache = { at: 0, map: null };
let dishMentionCountsCache = { at: 0, map: null };

export async function fetchCommentCounts() {
  if (!CLOUD_ENABLED) return new Map();
  if (commentCountsCache.map && Date.now() - commentCountsCache.at < 60_000) {
    return commentCountsCache.map;
  }
  const client = await getClient();
  const { data } = await client.from("comments").select("place_id").limit(1000);
  const map = new Map();
  for (const row of data || []) {
    map.set(row.place_id, (map.get(row.place_id) || 0) + 1);
  }
  commentCountsCache = { at: Date.now(), map };
  return map;
}

// `${place_id}:${dish_key}` -> number of comments that @mention that dish.
// The structured mentions array makes this stable even when numeric dish ids
// change after a menu refresh.
export async function fetchDishMentionCounts() {
  if (!CLOUD_ENABLED) return new Map();
  if (
    dishMentionCountsCache.map &&
    Date.now() - dishMentionCountsCache.at < 60_000
  ) {
    return dishMentionCountsCache.map;
  }
  const client = await getClient();
  const { data, error } = await client
    .from("comments")
    .select("place_id, mentions")
    .limit(1000);
  if (error) throw new Error(error.message);
  const map = new Map();
  for (const row of data || []) {
    for (const mention of row.mentions || []) {
      if (!mention?.dish_key) continue;
      const key = `${row.place_id}:${mention.dish_key}`;
      map.set(key, (map.get(key) || 0) + 1);
    }
  }
  dishMentionCountsCache = { at: Date.now(), map };
  return map;
}

export async function postComment(placeId, body, options) {
  const {
    dishMentions = [],
    userMentions = [],
    userId,
    parentCommentId = null,
  } = options || {};
  const client = await getClient();
  const { data, error } = await client
    .from("comments")
    .insert({
      user_id: userId,
      place_id: placeId,
      body,
      mentions: dishMentions,
      user_mentions: userMentions,
      parent_comment_id: parentCommentId,
    })
    .select(
      "id, user_id, body, mentions, user_mentions, parent_comment_id, created_at"
    )
    .single();
  if (error) throw new Error(error.message);
  commentCountsCache.at = 0; // card chips refresh on next fetch
  dishMentionCountsCache.at = 0;
  window.dispatchEvent(new Event("dishtune:comments-changed"));
  const [withName] = await attachUsernames(client, [data]);
  return withName;
}

export async function deleteComment(commentId) {
  const client = await getClient();
  const { error } = await client.from("comments").delete().eq("id", commentId);
  if (error) throw new Error(error.message);
  commentCountsCache.at = 0;
  dishMentionCountsCache.at = 0;
  window.dispatchEvent(new Event("dishtune:comments-changed"));
}

// ----------------------------------------------------------- notifications

// Everything the bell needs about you is derivable from the public comments
// table: replies target your note ids, mentions carry your user_id in the
// canonicalized user_mentions array (GIN-indexed for exactly this lookup).
export async function fetchNotificationEvents(userId) {
  if (!userId) return [];
  const client = await getClient();
  if (!client) return [];

  const { data: ownRows, error: ownError } = await client
    .from("comments")
    .select("id")
    .eq("user_id", userId)
    .order("created_at", { ascending: false })
    .limit(200);
  if (ownError) throw new Error(ownError.message);
  const ownIds = (ownRows || []).map((row) => row.id);

  const fields =
    "id, user_id, place_id, body, mentions, user_mentions, parent_comment_id, created_at";

  // Same bounded-URL chunking rationale as attachUsernames.
  const chunks = [];
  for (let index = 0; index < ownIds.length; index += 100) {
    chunks.push(ownIds.slice(index, index + 100));
  }
  const replyResponses = await Promise.all(
    chunks.map((ids) =>
      client
        .from("comments")
        .select(fields)
        .in("parent_comment_id", ids)
        .neq("user_id", userId)
        .order("created_at", { ascending: false })
        .limit(50)
    )
  );
  const replies = [];
  for (const { data, error } of replyResponses) {
    if (error) throw new Error(error.message);
    replies.push(...(data || []));
  }

  const { data: mentionRows, error: mentionError } = await client
    .from("comments")
    .select(fields)
    .contains("user_mentions", JSON.stringify([{ user_id: userId }]))
    .neq("user_id", userId)
    .order("created_at", { ascending: false })
    .limit(50);
  if (mentionError) throw new Error(mentionError.message);

  const merged = mergeNotificationEvents({
    replies,
    mentions: mentionRows || [],
    userId,
  }).slice(0, 50);
  return attachUsernames(client, merged);
}

export async function fetchMyNotes(userId) {
  if (!userId) return [];
  const client = await getClient();
  if (!client) return [];
  const { data, error } = await client
    .from("comments")
    .select("id, place_id, body, mentions, parent_comment_id, created_at")
    .eq("user_id", userId)
    .order("created_at", { ascending: false })
    .limit(200);
  if (error) throw new Error(error.message);
  return data || [];
}

// The notification_state migration can lag behind a frontend deploy.
// A missing table must degrade to "no watermark yet" (everything unread),
// never break the feed.
function isMissingNotificationState(error) {
  return (
    error?.code === "PGRST205" || // not in PostgREST's schema cache
    error?.code === "42P01" || // undefined_table
    /notification_state/.test(error?.message || "")
  );
}

export async function fetchNotificationsSeenAt(userId) {
  if (!userId) return null;
  const client = await getClient();
  if (!client) return null;
  const { data, error } = await client
    .from("notification_state")
    .select("seen_at")
    .eq("user_id", userId)
    .maybeSingle();
  if (error) {
    if (isMissingNotificationState(error)) return null;
    throw new Error(error.message);
  }
  return data?.seen_at || null;
}

export async function markNotificationsSeen(userId) {
  if (!userId) return null;
  const client = await getClient();
  if (!client) return null;
  const seenAt = new Date().toISOString();
  const { error } = await client
    .from("notification_state")
    .upsert({ user_id: userId, seen_at: seenAt });
  if (error) {
    if (isMissingNotificationState(error)) return null;
    throw new Error(error.message);
  }
  return seenAt;
}

export async function reportComment(commentId, userId) {
  const client = await getClient();
  const { error } = await client
    .from("comment_reports")
    .upsert({ comment_id: commentId, user_id: userId });
  if (error) throw new Error(error.message);
}
