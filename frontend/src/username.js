export const USERNAME_MIN_LENGTH = 3;
export const USERNAME_MAX_LENGTH = 20;
export const DEFAULT_PUBLIC_NAME = "Dish Explorer";

const RESERVED_USERNAMES = new Set([
  "admin",
  "administrator",
  "dishtune",
  "moderator",
  "official",
  "staff",
  "support",
]);

export function accountFallbackName(user) {
  return (
    user?.user_metadata?.full_name?.trim() ||
    user?.user_metadata?.name?.trim() ||
    user?.email?.split("@")[0]?.trim() ||
    "account"
  );
}

export function normalizeUsername(value) {
  return String(value || "")
    .trim()
    .replace(/^@+/, "")
    .toLowerCase();
}

export function validateUsername(value) {
  const username = normalizeUsername(value);
  if (!username) return { value: null, error: null };
  if (username.length < USERNAME_MIN_LENGTH) {
    return {
      value: username,
      error: `Use at least ${USERNAME_MIN_LENGTH} characters.`,
    };
  }
  if (username.length > USERNAME_MAX_LENGTH) {
    return {
      value: username,
      error: `Use ${USERNAME_MAX_LENGTH} characters or fewer.`,
    };
  }
  if (!/^[a-z0-9][a-z0-9_]*$/.test(username)) {
    return {
      value: username,
      error: "Use lowercase letters, numbers, and underscores; start with a letter or number.",
    };
  }
  if (RESERVED_USERNAMES.has(username)) {
    return {
      value: username,
      error: "That username is reserved. Please choose another.",
    };
  }
  return { value: username, error: null };
}

export function usernameLabel(username) {
  const normalized = normalizeUsername(username);
  return normalized ? `@${normalized}` : DEFAULT_PUBLIC_NAME;
}
