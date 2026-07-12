import test from "node:test";
import assert from "node:assert/strict";

import {
  attachReplyTargets,
  replyPreview,
} from "../src/commentReplies.js";

test("attaches a reply to a parent in the current thread", () => {
  const parent = { id: "note-1", body: "Original note" };
  const reply = {
    id: "note-2",
    body: "A response",
    parent_comment_id: "note-1",
  };
  const rows = attachReplyTargets([reply, parent]);
  assert.equal(rows[0].reply_to.id, "note-1");
  assert.equal(rows[1].reply_to, null);
});

test("uses an additionally fetched parent and tolerates a deleted one", () => {
  const reply = {
    id: "note-2",
    body: "A response",
    parent_comment_id: "older-note",
  };
  const [resolved] = attachReplyTargets(
    [reply],
    [{ id: "older-note", body: "Older note" }]
  );
  assert.equal(resolved.reply_to.body, "Older note");
  assert.equal(attachReplyTargets([reply])[0].reply_to, null);
});

test("builds a compact reply preview", () => {
  assert.equal(replyPreview("  Lots\n\nof   spacing  "), "Lots of spacing");
  assert.equal(replyPreview("123456789", 6), "12345…");
});
