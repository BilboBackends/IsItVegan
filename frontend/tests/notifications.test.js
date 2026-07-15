import test from "node:test";
import assert from "node:assert/strict";

import {
  groupNotesByPlace,
  isUnreadNotification,
  mergeNotificationEvents,
  unreadNotificationCount,
} from "../src/notifications.js";

const ME = "user-me";

test("merges replies and mentions newest-first without duplicates", () => {
  const events = mergeNotificationEvents({
    replies: [
      { id: "a", user_id: "them", created_at: "2026-07-14T10:00:00Z" },
      { id: "b", user_id: "them", created_at: "2026-07-15T09:00:00Z" },
    ],
    mentions: [
      // Also a reply: must appear once, labeled as the reply.
      { id: "b", user_id: "them", created_at: "2026-07-15T09:00:00Z" },
      { id: "c", user_id: "them", created_at: "2026-07-13T08:00:00Z" },
    ],
    userId: ME,
  });
  assert.deepEqual(
    events.map((event) => [event.id, event.kind]),
    [
      ["b", "reply"],
      ["a", "reply"],
      ["c", "mention"],
    ]
  );
});

test("drops your own notes from the feed", () => {
  const events = mergeNotificationEvents({
    replies: [{ id: "self", user_id: ME, created_at: "2026-07-15T00:00:00Z" }],
    mentions: [{ id: "self2", user_id: ME, created_at: "2026-07-15T00:00:00Z" }],
    userId: ME,
  });
  assert.equal(events.length, 0);
});

test("counts unread against the seen watermark", () => {
  const events = [
    { id: "old", created_at: "2026-07-10T00:00:00Z" },
    { id: "new", created_at: "2026-07-15T00:00:00Z" },
  ];
  assert.equal(unreadNotificationCount(events, "2026-07-12T00:00:00Z"), 1);
  assert.equal(unreadNotificationCount(events, null), 2);
  assert.equal(unreadNotificationCount([], "2026-07-12T00:00:00Z"), 0);
  assert.equal(
    isUnreadNotification(events[0], "2026-07-12T00:00:00Z"),
    false
  );
  assert.equal(isUnreadNotification(events[1], "2026-07-12T00:00:00Z"), true);
});

test("groups own notes by restaurant preserving recency order", () => {
  const groups = groupNotesByPlace([
    { id: "1", place_id: "p-new", created_at: "2026-07-15T00:00:00Z" },
    { id: "2", place_id: "p-old", created_at: "2026-07-14T00:00:00Z" },
    { id: "3", place_id: "p-new", created_at: "2026-07-13T00:00:00Z" },
  ]);
  assert.deepEqual(
    groups.map((group) => [group.place_id, group.notes.length]),
    [
      ["p-new", 2],
      ["p-old", 1],
    ]
  );
});
