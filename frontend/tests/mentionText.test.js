import test from "node:test";
import assert from "node:assert/strict";

import {
  hasDishMentionToken,
  hasUserMentionToken,
  mentionTriggerAt,
  replaceMentionTrigger,
  resolveUserMention,
} from "../src/mentionText.js";

test("finds and replaces a mention at the caret without losing suffix text", () => {
  const text = "Thanks @gre for the recommendation";
  const caret = text.indexOf(" for");
  const trigger = mentionTriggerAt(text, caret);
  assert.deepEqual(trigger, { start: 7, end: 11, query: "gre" });
  assert.deepEqual(replaceMentionTrigger(text, trigger, "green_pal"), {
    text: "Thanks @green_pal for the recommendation",
    caret: 17,
  });
});

test("does not trigger from inside an email address", () => {
  assert.equal(mentionTriggerAt("me@example.com"), null);
});

test("matches usernames with exact token boundaries", () => {
  assert.equal(hasUserMentionToken("thanks @alex!", "alex"), true);
  assert.equal(hasUserMentionToken("thanks @alexander", "alex"), false);
  assert.equal(hasUserMentionToken("(@ALEX)", "alex"), true);
});

test("keeps legacy dish mentions with regex punctuation intact", () => {
  assert.equal(hasDishMentionToken("Try @B.L.T. (V) today", "B.L.T. (V)"), true);
});

test("uses the current handle for renamed accounts", () => {
  assert.deepEqual(
    resolveUserMention({
      user_id: "person-1",
      username: "old_name",
      current_username: "new_name",
    }),
    {
      token: "@old_name",
      displayUsername: "new_name",
      mentionUsername: "new_name",
    }
  );
});

test("keeps removed historical handles visible but non-mentionable", () => {
  assert.deepEqual(
    resolveUserMention({ username: "old_name", current_username: null }),
    {
      token: "@old_name",
      displayUsername: "old_name",
      mentionUsername: null,
    }
  );
});
