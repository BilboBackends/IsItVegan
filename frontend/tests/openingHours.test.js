import test from "node:test";
import assert from "node:assert/strict";

import { scheduledOpenState, todayHoursText } from "../src/openingHours.js";

const zone = "America/New_York";

test("reads split shifts and their closed gap", () => {
  const hours = ["Monday: 11:00 AM–2:00 PM, 5:00–10:00 PM"];
  assert.equal(scheduledOpenState(hours, new Date("2026-07-06T17:00:00Z"), zone), true);
  assert.equal(scheduledOpenState(hours, new Date("2026-07-06T19:00:00Z"), zone), false);
  assert.equal(scheduledOpenState(hours, new Date("2026-07-07T01:00:00Z"), zone), true);
});

test("infers omitted AM/PM in Google's compact same-period format", () => {
  const hours = ["Monday: 12:00 – 9:00 PM"];
  assert.equal(scheduledOpenState(hours, new Date("2026-07-07T00:00:00Z"), zone), true);
  assert.equal(scheduledOpenState(hours, new Date("2026-07-07T02:00:00Z"), zone), false);
});

test("carries an overnight interval into the following day", () => {
  const hours = ["Friday: 5:00 PM–2:00 AM", "Saturday: Closed"];
  assert.equal(scheduledOpenState(hours, new Date("2026-07-11T05:00:00Z"), zone), true);
  assert.equal(scheduledOpenState(hours, new Date("2026-07-11T07:00:00Z"), zone), false);
});

test("handles 24-hour schedules and returns today's display text", () => {
  const hours = ["Sunday: Open 24 hours"];
  const date = new Date("2026-07-05T16:00:00Z");
  assert.equal(scheduledOpenState(hours, date, zone), true);
  assert.equal(todayHoursText(hours, date, zone), "Open 24 hours");
});
