import test from "node:test";
import assert from "node:assert/strict";

import {
  DISH_DETAIL_HISTORY_KEY,
  isOwnedDishDetailRoute,
  pushDishDetailRoute,
  replaceHashRoute,
  replaceHashRouteFromClick,
} from "../src/hashNavigation.js";

function fakeWindow(initialUrl, initialState = null) {
  let currentUrl = new URL(initialUrl);
  const calls = { push: [], replace: [], events: [] };
  const location = {};
  Object.defineProperties(location, {
    href: { get: () => currentUrl.toString() },
    hash: { get: () => currentUrl.hash },
  });
  const history = {
    state: initialState,
    pushState(state, _title, url) {
      this.state = state;
      currentUrl = new URL(url, currentUrl);
      calls.push.push({ state, url: currentUrl.toString() });
    },
    replaceState(state, _title, url) {
      this.state = state;
      currentUrl = new URL(url, currentUrl);
      calls.replace.push({ state, url: currentUrl.toString() });
    },
  };
  class FakeEvent {
    constructor(type) {
      this.type = type;
    }
  }
  return {
    win: {
      location,
      history,
      Event: FakeEvent,
      dispatchEvent(event) {
        calls.events.push(event.type);
      },
    },
    calls,
  };
}

function click(overrides = {}) {
  return {
    button: 0,
    defaultPrevented: false,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
    ...overrides,
  };
}

test("primary tab navigation replaces the current hash without losing query or state", () => {
  const detailState = {
    account: "kept",
    [DISH_DETAIL_HISTORY_KEY]: {
      hash: "#dishes?dish=12",
      returnHash: "#saved",
    },
  };
  const { win, calls } = fakeWindow(
    "https://dishtune.com/?comments=place#dishes?dish=12",
    detailState
  );

  assert.equal(replaceHashRoute("restaurants", win), true);
  assert.equal(win.location.href, "https://dishtune.com/?comments=place#restaurants");
  assert.deepEqual(win.history.state, { account: "kept" });
  assert.equal(calls.replace.length, 1);
  assert.equal(calls.push.length, 0);
  assert.deepEqual(calls.events, ["hashchange"]);
});

test("tab click replacement preserves modified-click browser behavior", () => {
  const { win, calls } = fakeWindow("https://dishtune.com/#restaurants");
  const modifiedClick = click({ ctrlKey: true });

  assert.equal(
    replaceHashRouteFromClick(modifiedClick, "dishes", win),
    false
  );
  assert.equal(modifiedClick.prevented, false);
  assert.equal(win.location.hash, "#restaurants");
  assert.equal(calls.replace.length, 0);
});

test("a locally opened dish records a safe return route", () => {
  const { win, calls } = fakeWindow("https://dishtune.com/explore?x=1#saved", {
    account: "kept",
  });

  assert.equal(pushDishDetailRoute(42, win), true);
  assert.equal(win.location.href, "https://dishtune.com/explore?x=1#dishes?dish=42");
  assert.equal(isOwnedDishDetailRoute(win), true);
  assert.deepEqual(win.history.state, {
    account: "kept",
    [DISH_DETAIL_HISTORY_KEY]: {
      hash: "#dishes?dish=42",
      returnHash: "#saved",
    },
  });
  assert.equal(calls.push.length, 1);
  assert.deepEqual(calls.events, ["hashchange"]);
});

test("replacing a dish detail route clears its owned-overlay marker", () => {
  const { win, calls } = fakeWindow(
    "https://dishtune.com/#dishes?dish=42",
    {
      [DISH_DETAIL_HISTORY_KEY]: {
        hash: "#dishes?dish=42",
        returnHash: "#dishes",
      },
    }
  );

  replaceHashRoute("dishes", win);
  assert.equal(win.location.hash, "#dishes");
  assert.equal(isOwnedDishDetailRoute(win), false);
  assert.equal(calls.replace.length, 1);
});
