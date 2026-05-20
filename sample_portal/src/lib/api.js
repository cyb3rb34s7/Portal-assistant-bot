/* HTTP wrapper used by all pages.
 *
 * Reads the bearer token from sessionStorage so a refresh keeps the
 * operator logged in. Every state-changing call accepts an optional
 * idempotencyKey -- callers that want retry-safety pass one; callers
 * that don't, don't. Nothing here knows about specific endpoints; it's
 * just transport. */

const TOKEN_KEY = "sample_portal_token";

export function getToken() {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || null;
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
}

async function http(method, path, { body, idempotencyKey } = {}) {
  const headers = { "Content-Type": "application/json" };
  const tok = getToken();
  if (tok) headers["Authorization"] = `Bearer ${tok}`;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const init = { method, headers };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(path, init);
  let payload = null;
  try {
    payload = await resp.json();
  } catch {
    payload = null;
  }
  if (!resp.ok) {
    const err = new Error(
      (payload && (payload.error || payload.message)) ||
        `HTTP ${resp.status}`,
    );
    err.status = resp.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

export const api = {
  get: (p) => http("GET", p),
  post: (p, body, opts) => http("POST", p, { body, ...opts }),
  patch: (p, body, opts) => http("PATCH", p, { body, ...opts }),
};

// Convenience idempotency-key generator. Real portals would use UUID;
// here we just need uniqueness within a session.
export function newIdempotencyKey() {
  return (
    Math.random().toString(36).slice(2, 12) +
    Date.now().toString(36)
  );
}
