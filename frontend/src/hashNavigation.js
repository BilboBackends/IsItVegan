export const DISH_DETAIL_HISTORY_KEY = "dishTuneDishDetail";

function normalizedHash(hash) {
  const value = String(hash || "").replace(/^#/, "");
  return `#${value}`;
}

function urlWithHash(location, hash) {
  const url = new URL(location.href);
  url.hash = normalizedHash(hash);
  return url.toString();
}

function withoutDishDetailState(state) {
  if (!state || typeof state !== "object" || !(DISH_DETAIL_HISTORY_KEY in state)) {
    return state;
  }
  const next = { ...state };
  delete next[DISH_DETAIL_HISTORY_KEY];
  return next;
}

function notifyHashChange(win) {
  // pushState/replaceState intentionally do not emit hashchange themselves.
  // The app's hash router only needs a prompt to reread window.location.
  win.dispatchEvent(new win.Event("hashchange"));
}

export function replaceHashRoute(hash, win = window) {
  const target = normalizedHash(hash);
  const hashChanged = win.location.hash !== target;
  const nextState = withoutDishDetailState(win.history.state);
  const stateChanged = nextState !== win.history.state;
  if (!hashChanged && !stateChanged) return false;

  win.history.replaceState(
    nextState,
    "",
    urlWithHash(win.location, target)
  );
  if (hashChanged) notifyHashChange(win);
  return true;
}

function isPlainPrimaryClick(event) {
  return (
    !event.defaultPrevented &&
    (event.button == null || event.button === 0) &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

export function replaceHashRouteFromClick(event, hash, win = window) {
  if (!isPlainPrimaryClick(event)) return false;
  event.preventDefault();
  replaceHashRoute(hash, win);
  return true;
}

export function pushDishDetailRoute(dishId, win = window) {
  const id = Number(dishId);
  if (!Number.isInteger(id) || id <= 0) return false;
  const target = `#dishes?dish=${id}`;
  if (win.location.hash === target) return false;

  const priorState =
    win.history.state && typeof win.history.state === "object"
      ? win.history.state
      : {};
  win.history.pushState(
    {
      ...priorState,
      [DISH_DETAIL_HISTORY_KEY]: {
        hash: target,
        returnHash: win.location.hash || "#restaurants",
      },
    },
    "",
    urlWithHash(win.location, target)
  );
  notifyHashChange(win);
  return true;
}

export function pushDishDetailRouteFromClick(event, dishId, win = window) {
  if (!isPlainPrimaryClick(event)) return false;
  event.preventDefault();
  pushDishDetailRoute(dishId, win);
  return true;
}

export function isOwnedDishDetailRoute(win = window) {
  const detail = win.history.state?.[DISH_DETAIL_HISTORY_KEY];
  return Boolean(detail && detail.hash === win.location.hash);
}
