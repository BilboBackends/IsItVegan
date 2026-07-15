// Pure logic for the header notification bell: merging reply/mention rows
// into one feed, counting unread against the account's seen_at watermark,
// and grouping a user's own notes by restaurant. Kept dependency-free so it
// runs under `node --test` like the other logic modules.

// A note can both reply to you and @mention you; it must appear once. The
// reply relationship is the more specific one, so it wins the label.
export function mergeNotificationEvents({ replies = [], mentions = [], userId = null }) {
  const byId = new Map();
  for (const row of mentions) {
    if (!row?.id || row.user_id === userId) continue;
    byId.set(row.id, { ...row, kind: "mention" });
  }
  for (const row of replies) {
    if (!row?.id || row.user_id === userId) continue;
    byId.set(row.id, { ...row, kind: "reply" });
  }
  return [...byId.values()].sort(
    (a, b) => new Date(b.created_at) - new Date(a.created_at)
  );
}

// seen_at is a cross-device watermark; a null (never opened the panel)
// means everything counts as unread.
export function unreadNotificationCount(events, seenAt) {
  if (!events?.length) return 0;
  const seenTime = seenAt ? new Date(seenAt).getTime() : 0;
  return events.filter((event) => {
    const at = new Date(event.created_at).getTime();
    return Number.isFinite(at) && at > seenTime;
  }).length;
}

export function isUnreadNotification(event, seenAt) {
  if (!event?.created_at) return false;
  const seenTime = seenAt ? new Date(seenAt).getTime() : 0;
  return new Date(event.created_at).getTime() > seenTime;
}

// "Your notes" tab: one section per restaurant, ordered by each section's
// newest note. Callers pass notes already sorted newest-first, so the first
// appearance of a place_id fixes its section's rank.
export function groupNotesByPlace(notes) {
  const groups = new Map();
  for (const note of notes || []) {
    if (!note?.place_id) continue;
    if (!groups.has(note.place_id)) groups.set(note.place_id, []);
    groups.get(note.place_id).push(note);
  }
  return [...groups.entries()].map(([placeId, placeNotes]) => ({
    place_id: placeId,
    notes: placeNotes,
  }));
}
