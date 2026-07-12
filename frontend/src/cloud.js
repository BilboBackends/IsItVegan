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

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL;
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const CLOUD_ENABLED = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);
export const GOOGLE_AUTH_ENABLED =
  import.meta.env.VITE_SUPABASE_GOOGLE_ENABLED === "true";

// The signed-in Supabase session (or null), provided from App so deep
// components (thumbs, comments) don't need prop-drilling.
export const SessionContext = createContext(null);

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

export async function signInWithGoogle() {
  const client = await getClient();
  await client?.auth.signInWithOAuth({
    provider: "google",
    options: { redirectTo: window.location.origin + window.location.pathname },
  });
}

export async function signInWithMagicLink(email) {
  const client = await getClient();
  const { error } = await client.auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: window.location.origin + window.location.pathname,
    },
  });
  if (error) throw new Error(error.message);
}

export async function signOut() {
  const client = await getClient();
  await client?.auth.signOut();
}

// --------------------------------------------------------------- favorites

// Pull the account's favorites as {dishes: [ids], restaurants: [ids]},
// preferring registry resolution (survives id renumbering) and falling back
// to the stored local_id hint.
export async function pullFavorites() {
  const client = await getClient();
  if (!client) return null;
  const { data, error } = await client.from("favorites").select("*");
  if (error || !data) return null;
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

// Display names are stitched in with a second query instead of a PostgREST
// relationship embed: duplicate FKs (schema.sql + migrations both applied)
// make embeds ambiguous ("more than one relationship was found"), and the
// stitch works regardless of the database's constraint history.
async function attachDisplayNames(client, comments) {
  const userIds = [...new Set(comments.map((c) => c.user_id))];
  if (userIds.length === 0) return comments;
  const names = new Map();
  const { data } = await client
    .from("profiles")
    .select("id, display_name")
    .in("id", userIds);
  for (const row of data || []) names.set(row.id, row.display_name);
  return comments.map((c) => ({
    ...c,
    profiles: { display_name: names.get(c.user_id) || "vegan explorer" },
  }));
}

export async function fetchComments(placeId) {
  const client = await getClient();
  if (!client) return [];
  const { data, error } = await client
    .from("comments")
    .select("id, user_id, body, mentions, created_at")
    .eq("place_id", placeId)
    .order("created_at", { ascending: false })
    .limit(100);
  if (error) throw new Error(error.message);
  return attachDisplayNames(client, data || []);
}

// place_id -> comment count, for the 💬 chips on restaurant cards. Counting
// client-side over a bare place_id column is fine at MVP scale (PostgREST
// caps the response at 1000 rows); switch to a count() aggregate or a view
// when threads outgrow that.
let commentCountsCache = { at: 0, map: null };

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

export async function postComment(placeId, body, mentions, userId) {
  const client = await getClient();
  const { data, error } = await client
    .from("comments")
    .insert({
      user_id: userId,
      place_id: placeId,
      body,
      mentions: mentions || [],
    })
    .select("id, user_id, body, mentions, created_at")
    .single();
  if (error) throw new Error(error.message);
  commentCountsCache.at = 0; // card chips refresh on next fetch
  const [withName] = await attachDisplayNames(client, [data]);
  return withName;
}

export async function deleteComment(commentId) {
  const client = await getClient();
  const { error } = await client.from("comments").delete().eq("id", commentId);
  if (error) throw new Error(error.message);
  commentCountsCache.at = 0;
}

export async function reportComment(commentId, userId) {
  const client = await getClient();
  const { error } = await client
    .from("comment_reports")
    .upsert({ comment_id: commentId, user_id: userId });
  if (error) throw new Error(error.message);
}
