export function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function mentionTriggerAt(text, caret = text.length) {
  const beforeCaret = String(text).slice(0, caret);
  const match = /(?:^|[^a-z0-9_@])@([^@\n]{0,40})$/i.exec(beforeCaret);
  if (!match) return null;
  const start = beforeCaret.lastIndexOf("@");
  return { start, end: caret, query: match[1] };
}

export function replaceMentionTrigger(text, trigger, label) {
  if (!trigger) return { text, caret: String(text).length };
  const suffix = String(text).slice(trigger.end);
  const insertion = `@${label}${/^\s/.test(suffix) ? "" : " "}`;
  return {
    text:
      String(text).slice(0, trigger.start) +
      insertion +
      suffix,
    caret: trigger.start + insertion.length,
  };
}

export function hasUserMentionToken(text, username) {
  if (!username) return false;
  return new RegExp(
    `(^|[^a-z0-9_])@${escapeRegExp(username)}(?![a-z0-9_])`,
    "i"
  ).test(String(text));
}

export function hasDishMentionToken(text, dishName) {
  return dishName ? String(text).includes(`@${dishName}`) : false;
}

// The body keeps the username that was selected when the note was written,
// while user_id remains the durable identity. Fetches add current_username so
// a renamed account stays mentionable and a removed handle is not a dead link.
export function resolveUserMention(mention) {
  const snapshotUsername = String(mention?.username || "").trim();
  if (!snapshotUsername) return null;
  const hasCurrentUsername = Object.prototype.hasOwnProperty.call(
    mention,
    "current_username"
  );
  const currentUsername = hasCurrentUsername
    ? String(mention.current_username || "").trim() || null
    : snapshotUsername;
  return {
    token: `@${snapshotUsername}`,
    displayUsername: currentUsername || snapshotUsername,
    mentionUsername: currentUsername,
  };
}
