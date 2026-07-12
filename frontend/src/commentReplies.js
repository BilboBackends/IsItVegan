export const REPLY_PREVIEW_MAX_LENGTH = 96;

export function replyPreview(body, maxLength = REPLY_PREVIEW_MAX_LENGTH) {
  const compact = String(body || "").replace(/\s+/g, " ").trim();
  if (compact.length <= maxLength) return compact;
  return `${compact.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}

// Main thread queries are capped, so an older parent may need to be fetched
// separately. Keep the ID relationship intact while giving each reply a
// lightweight parent object for its context row.
export function attachReplyTargets(comments, extraParents = []) {
  const parentsById = new Map();
  for (const parent of [...extraParents, ...comments]) {
    if (parent?.id) parentsById.set(parent.id, parent);
  }
  return comments.map((comment) => ({
    ...comment,
    reply_to: comment.parent_comment_id
      ? parentsById.get(comment.parent_comment_id) || null
      : null,
  }));
}
