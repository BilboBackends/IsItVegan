import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_PUBLIC_NAME,
  accountFallbackName,
  normalizeUsername,
  usernameLabel,
  validateUsername,
} from "../src/username.js";

test("uses private auth identity only as the signed-in account fallback", () => {
  assert.equal(
    accountFallbackName({
      user_metadata: { full_name: "  Alex Rivera  " },
      email: "email-name@example.com",
    }),
    "Alex Rivera"
  );
  assert.equal(
    accountFallbackName({ user_metadata: {}, email: "email-name@example.com" }),
    "email-name"
  );
  assert.equal(accountFallbackName(null), "account");
});

test("normalizes an optional username", () => {
  assert.equal(normalizeUsername("  @Green_Pal "), "green_pal");
  assert.deepEqual(validateUsername("   "), { value: null, error: null });
  assert.equal(usernameLabel(null), DEFAULT_PUBLIC_NAME);
  assert.equal(usernameLabel("green_pal"), "@green_pal");
});

test("accepts the 3 and 20 character boundaries", () => {
  assert.equal(validateUsername("abc").error, null);
  assert.equal(validateUsername("a".repeat(20)).error, null);
  assert.match(validateUsername("ab").error, /at least 3/);
  assert.match(validateUsername("a".repeat(21)).error, /20/);
});

test("rejects invalid and reserved usernames", () => {
  assert.match(validateUsername("two words").error, /letters, numbers/);
  assert.match(validateUsername("_starts_wrong").error, /start with/);
  assert.match(validateUsername("dish-tune").error, /letters, numbers/);
  assert.match(validateUsername("ADMIN").error, /reserved/);
  assert.match(validateUsername("DishTune").error, /reserved/);
});
