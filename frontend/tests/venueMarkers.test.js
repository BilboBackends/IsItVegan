import test from "node:test";
import assert from "node:assert/strict";

import { venueMarkerHtml } from "../src/venueMarkers.js";

test("renders a typed marker while preserving its numeric count", () => {
  const html = venueMarkerHtml({
    kind: "dessert",
    count: 12,
    color: "#047857",
  });
  assert.match(html, /vf-place-pin__type--dessert/);
  assert.match(html, /vf-place-pin__count">12</);
  assert.match(html, /--vf-pin-color:#047857/);
});

test("uses a question mark and safe fallbacks for unanalyzed places", () => {
  const html = venueMarkerHtml({
    kind: "unknown",
    count: "<script>",
    color: "not-a-color",
    analyzed: false,
  });
  assert.match(html, /vf-place-pin__type--restaurant/);
  assert.match(html, /vf-place-pin__count">\?</);
  assert.match(html, /--vf-pin-color:#78716c/);
  assert.doesNotMatch(html, /script/);
});
