/* Mock backend for the sample portal.
 *
 * In-memory state machine + Vite middleware. Real HTTP, real
 * latency, real Idempotency-Key handling. NOT hardcoded for any
 * specific test scenario -- the data is seeded once at startup and
 * mutated by API calls like any backend.
 *
 * The portal exercises:
 *   - bearer-token auth (the auth_signal probe sees the user menu)
 *   - cascading reference data (regions -> markets -> languages)
 *   - multi-select reference data (categories, tags)
 *   - server-side asset state machine: draft -> in_review ->
 *     (approved | rejected) -> published
 *   - idempotency on every state-changing endpoint
 *   - paginated search with prefix-like matching (the multi-result
 *     scenario from the operator's real-portal issue)
 */

// ----- Configuration --------------------------------------------------
//
// F-08f: named constants for behavior that downstream tests / fixtures
// may want to reason about. Burying these inside function bodies makes
// it look like the values are hardcoded for tests; lifting them to the
// top makes the contract explicit.

// IDEMPOTENCY_WINDOW_MS: how long an idempotency key stays in the
// cache. 60s matches the runner's per-step retry budget: a retry of
// the same logical step within this window dedupes to the cached
// response. Set well below the typical session duration so stale
// keys eventually evict and don't bloat memory.
const IDEMPOTENCY_WINDOW_MS = 60_000;

// ----- Seed data ------------------------------------------------------

const REGIONS = [
  { code: "AMER", name: "Americas" },
  { code: "EMEA", name: "Europe / Middle East / Africa" },
  { code: "APAC", name: "Asia Pacific" },
];

// Markets are children of regions. Operator picks region, the market
// dropdown repopulates. Classic cascading-enum problem.
const MARKETS = {
  AMER: [
    { code: "US", name: "United States" },
    { code: "CA", name: "Canada" },
    { code: "BR", name: "Brazil" },
    { code: "MX", name: "Mexico" },
  ],
  EMEA: [
    { code: "GB", name: "United Kingdom" },
    { code: "DE", name: "Germany" },
    { code: "FR", name: "France" },
    { code: "AE", name: "United Arab Emirates" },
  ],
  APAC: [
    { code: "IN", name: "India" },
    { code: "JP", name: "Japan" },
    { code: "AU", name: "Australia" },
    { code: "SG", name: "Singapore" },
  ],
};

// Languages depend on the market choice. Indian market has Hindi +
// English + Tamil; Japanese market has Japanese + English; etc.
const LANGUAGES = {
  US: [{ code: "en-US", name: "English (US)" }],
  CA: [
    { code: "en-CA", name: "English (Canada)" },
    { code: "fr-CA", name: "French (Canada)" },
  ],
  BR: [{ code: "pt-BR", name: "Portuguese (Brazil)" }],
  MX: [{ code: "es-MX", name: "Spanish (Mexico)" }],
  GB: [{ code: "en-GB", name: "English (UK)" }],
  DE: [
    { code: "de-DE", name: "German" },
    { code: "en-GB", name: "English (UK)" },
  ],
  FR: [{ code: "fr-FR", name: "French" }],
  AE: [
    { code: "ar-AE", name: "Arabic" },
    { code: "en-GB", name: "English (UK)" },
  ],
  IN: [
    { code: "hi-IN", name: "Hindi" },
    { code: "en-IN", name: "English (India)" },
    { code: "ta-IN", name: "Tamil" },
  ],
  JP: [
    { code: "ja-JP", name: "Japanese" },
    { code: "en-US", name: "English (US)" },
  ],
  AU: [{ code: "en-AU", name: "English (Australia)" }],
  SG: [
    { code: "en-SG", name: "English (Singapore)" },
    { code: "zh-SG", name: "Chinese (Singapore)" },
  ],
};

const CATEGORIES = [
  { id: "sports", name: "Sports" },
  { id: "news", name: "News" },
  { id: "entertainment", name: "Entertainment" },
  { id: "kids", name: "Kids" },
  { id: "documentary", name: "Documentary" },
  { id: "drama", name: "Drama" },
  { id: "comedy", name: "Comedy" },
  { id: "music", name: "Music" },
];

const TAGS = [
  { id: "hd", name: "HD" },
  { id: "4k", name: "4K" },
  { id: "dolby", name: "Dolby Audio" },
  { id: "ad", name: "Audio Description" },
  { id: "cc", name: "Closed Captions" },
  { id: "live", name: "Live" },
  { id: "exclusive", name: "Exclusive" },
];

// Seed catalog -- mix of overlapping prefixes so search "A-90" returns
// multiple rows (the operator's real-world multi-match scenario).
function seedAssets() {
  const assets = [
    { id: "A-9001", title: "Spring Cup Final 2026" },
    { id: "A-9002", title: "Spring Cup Highlights" },
    { id: "A-9003", title: "Spring Cup Preview" },
    { id: "A-1001", title: "Winter Games Opening" },
    { id: "A-1002", title: "Winter Games Highlights" },
    { id: "B-2001", title: "Evening News -- Anchor A" },
    { id: "B-2002", title: "Evening News -- Anchor B" },
    { id: "C-3001", title: "Family Comedy Pilot" },
    { id: "D-4001", title: "Documentary: Oceans" },
    { id: "D-4002", title: "Documentary: Cities" },
  ];
  for (const a of assets) {
    a.categories = []; // []ids
    a.tags = []; // []ids
    a.region = null;
    a.market = null;
    a.language = null;
    a.publish_at = null;
    a.comment = "";
    a.status = "draft"; // draft -> in_review -> (approved | rejected) -> published
    a.workflow_history = [];
  }
  return assets;
}

// ----- In-memory state (per Vite server lifetime) -----

const state = {
  assets: seedAssets(),
  // session_token -> { username, issued_at }
  sessions: new Map(),
  // Idempotency-Key -> { ts, response_body, status }
  idempotency_cache: new Map(),
};

// ----- Helpers --------------------------------------------------------

function json(res, status, body, extraHeaders = {}) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  for (const [k, v] of Object.entries(extraHeaders)) res.setHeader(k, v);
  res.end(JSON.stringify(body));
}

function send(res, status, body, delayMs, extraHeaders) {
  setTimeout(() => json(res, status, body, extraHeaders), delayMs || 0);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let buf = "";
    req.on("data", (chunk) => (buf += chunk));
    req.on("end", () => {
      if (!buf) return resolve({});
      try {
        resolve(JSON.parse(buf));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function authedUser(req) {
  const h = req.headers["authorization"] || "";
  if (!h.startsWith("Bearer ")) return null;
  const tok = h.slice(7);
  const sess = state.sessions.get(tok);
  if (!sess) return null;
  return sess;
}

function requireAuth(req, res) {
  const u = authedUser(req);
  if (!u) {
    json(res, 401, { error: "unauthorized" });
    return null;
  }
  return u;
}

// Idempotency: cache by header value. Within 60s, repeats return the
// cached response. NOT hardcoded for any test -- works for every
// state-changing endpoint that opts in.
function maybeReplayIdempotent(req, res) {
  const key = req.headers["idempotency-key"];
  if (!key) return null;
  const cached = state.idempotency_cache.get(key);
  if (!cached) return null;
  if (Date.now() - cached.ts > IDEMPOTENCY_WINDOW_MS) {
    state.idempotency_cache.delete(key);
    return null;
  }
  json(res, cached.status, cached.body, { "x-idempotent-replay": "true" });
  return true;
}

function cacheIdempotent(req, status, body) {
  const key = req.headers["idempotency-key"];
  if (!key) return;
  state.idempotency_cache.set(key, { ts: Date.now(), status, body });
}

// ----- Route table ---------------------------------------------------

async function dispatch(req, res, url) {
  const path = url.pathname;
  const method = req.method;

  // ---- Auth ----
  if (path === "/api/auth/login" && method === "POST") {
    const body = await readBody(req);
    // Any non-empty username/password works -- this is a sample portal.
    // Real portals would validate against a directory, OIDC, etc.
    if (!body.username || !body.password) {
      return send(res, 400, { error: "username + password required" }, 500);
    }
    const token = "tok_" + Math.random().toString(36).slice(2, 14);
    state.sessions.set(token, {
      username: body.username,
      issued_at: Date.now(),
    });
    // Heavy 2.5s -- mimics real-portal SSO redirect + token issuance.
    return send(
      res,
      200,
      { token, user: { username: body.username } },
      2500,
    );
  }
  if (path === "/api/auth/me" && method === "GET") {
    const u = requireAuth(req, res);
    if (!u) return;
    return send(res, 200, { user: { username: u.username } }, 200);
  }
  if (path === "/api/auth/logout" && method === "POST") {
    const h = req.headers["authorization"] || "";
    if (h.startsWith("Bearer ")) state.sessions.delete(h.slice(7));
    return send(res, 200, { ok: true }, 500);
  }

  // ---- Reference data: cascading + multi-select sources ----
  // Latencies tuned so the cascade is *visible* during teach and
  // unavoidable during replay -- the runner has to wait for each
  // dropdown to repopulate or it'll race.
  if (path === "/api/regions" && method === "GET") {
    return send(res, 200, REGIONS, 800);
  }
  if (path === "/api/markets" && method === "GET") {
    if (!requireAuth(req, res)) return;
    const region = url.searchParams.get("region");
    const list = MARKETS[region] || [];
    return send(res, 200, list, 2000);
  }
  if (path === "/api/languages" && method === "GET") {
    if (!requireAuth(req, res)) return;
    const market = url.searchParams.get("market");
    const list = LANGUAGES[market] || [];
    return send(res, 200, list, 1500);
  }
  if (path === "/api/categories" && method === "GET") {
    return send(res, 200, CATEGORIES, 700);
  }
  if (path === "/api/tags" && method === "GET") {
    return send(res, 200, TAGS, 600);
  }

  // ---- Catalog search (multi-result scenario) ----
  if (path === "/api/catalog/assets" && method === "GET") {
    if (!requireAuth(req, res)) return;
    const q = (url.searchParams.get("q") || "").trim().toUpperCase();
    const category = url.searchParams.get("category");
    let rows = state.assets;
    if (q) {
      rows = rows.filter(
        (a) =>
          a.id.toUpperCase().includes(q) ||
          a.title.toUpperCase().includes(q),
      );
    }
    if (category) {
      rows = rows.filter((a) => a.categories.includes(category));
    }
    // Return a stable subset; "id"/"title" only, full asset via /api/assets/:id.
    const summary = rows.map((a) => ({
      id: a.id,
      title: a.title,
      status: a.status,
    }));
    // Catalog search is heavy -- a real portal hits an index + paginates.
    return send(res, 200, { rows: summary, total: summary.length, q }, 2500);
  }

  // ---- Asset detail + mutations ----
  // State-changing endpoints are deliberately slow (2-3s) so:
  //   (a) the operator can see the spinner / "Saving..." UI fire,
  //   (b) the replay-time wait predicates have to actually wait,
  //   (c) the diagnostic event records realistic waits_ms.
  const assetMatch = path.match(/^\/api\/assets\/([^/]+)(\/[^?]*)?$/);
  if (assetMatch) {
    if (!requireAuth(req, res)) return;
    const id = assetMatch[1];
    const sub = assetMatch[2] || "";
    const asset = state.assets.find((a) => a.id === id);
    if (!asset) return send(res, 404, { error: "asset not found" }, 300);

    if (!sub && method === "GET") {
      return send(res, 200, asset, 1200);
    }
    if (!sub && method === "PATCH") {
      if (maybeReplayIdempotent(req, res)) return;
      const body = await readBody(req);
      // WI-44: server validation. Title is required (non-empty,
      // trimmed). Return 422 with a field-keyed error so the
      // client can mark the title input aria-invalid and the
      // runner's _check_validation_errors auto-detect surfaces
      // 'Title is required' as the failure reason.
      if ("title" in body && (!body.title || !String(body.title).trim())) {
        const errBody = {
          error: "validation_failed",
          fields: {
            title: "Title is required",
          },
        };
        cacheIdempotent(req, 422, errBody);
        return send(res, 422, errBody, 500);
      }
      // Whitelist of editable fields -- never trust client to set
      // status / workflow_history directly.
      const editable = [
        "title",
        "categories",
        "tags",
        "region",
        "market",
        "language",
        "publish_at",
        "comment",
        "description",
        "priority",
      ];
      for (const k of editable) {
        if (k in body) asset[k] = body[k];
      }
      cacheIdempotent(req, 200, asset);
      return send(res, 200, asset, 2500);
    }
    if (sub === "/submit_review" && method === "POST") {
      if (maybeReplayIdempotent(req, res)) return;
      if (asset.status !== "draft") {
        const errBody = {
          error: "invalid_state",
          current_status: asset.status,
        };
        cacheIdempotent(req, 409, errBody);
        return send(res, 409, errBody, 600);
      }
      asset.status = "in_review";
      asset.workflow_history.push({
        ts: Date.now(),
        from: "draft",
        to: "in_review",
        by: authedUser(req).username,
      });
      cacheIdempotent(req, 200, asset);
      return send(res, 200, asset, 2500);
    }
    if (sub === "/approve" && method === "POST") {
      if (maybeReplayIdempotent(req, res)) return;
      if (asset.status !== "in_review") {
        const errBody = {
          error: "invalid_state",
          current_status: asset.status,
        };
        cacheIdempotent(req, 409, errBody);
        return send(res, 409, errBody, 600);
      }
      asset.status = "approved";
      asset.workflow_history.push({
        ts: Date.now(),
        from: "in_review",
        to: "approved",
        by: authedUser(req).username,
      });
      cacheIdempotent(req, 200, asset);
      return send(res, 200, asset, 2200);
    }
    if (sub === "/reject" && method === "POST") {
      if (maybeReplayIdempotent(req, res)) return;
      if (asset.status !== "in_review") {
        const errBody = {
          error: "invalid_state",
          current_status: asset.status,
        };
        cacheIdempotent(req, 409, errBody);
        return send(res, 409, errBody, 600);
      }
      const body = await readBody(req);
      asset.status = "draft";
      asset.workflow_history.push({
        ts: Date.now(),
        from: "in_review",
        to: "draft",
        by: authedUser(req).username,
        reason: body.reason || "no reason",
      });
      cacheIdempotent(req, 200, asset);
      return send(res, 200, asset, 2000);
    }
    if (sub === "/publish" && method === "POST") {
      if (maybeReplayIdempotent(req, res)) return;
      if (asset.status !== "approved") {
        const errBody = {
          error: "invalid_state",
          current_status: asset.status,
        };
        cacheIdempotent(req, 409, errBody);
        return send(res, 409, errBody, 600);
      }
      asset.status = "published";
      asset.workflow_history.push({
        ts: Date.now(),
        from: "approved",
        to: "published",
        by: authedUser(req).username,
      });
      cacheIdempotent(req, 200, asset);
      // Publish is the heaviest -- this is the destructive irreversible
      // action a real portal would queue + replicate across regions.
      return send(res, 200, asset, 3000);
    }
  }

  // ---- Save / Apply (legacy curation page) ----
  if (path === "/api/save" && method === "POST") {
    return send(res, 200, { ok: true, ts: Date.now() }, 2500);
  }
  if (path === "/api/apply" && method === "POST") {
    return send(res, 200, { ok: true, ts: Date.now() }, 3000);
  }
  if (path === "/api/search") {
    // Legacy search endpoint (used by the original Curation page).
    const q = (url.searchParams.get("q") || "").trim().toUpperCase();
    const rows = state.assets
      .filter((a) => a.id.toUpperCase().includes(q))
      .map((a) => ({ content_id: a.id, title: a.title }));
    return send(res, 200, { rows, total: rows.length, q }, 2500);
  }

  return null; // not handled
}

export function mockApiPlugin() {
  return {
    name: "mock-enterprise-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url || !req.url.startsWith("/api/")) return next();
        const url = new URL(req.url, "http://localhost");
        try {
          const handled = await dispatch(req, res, url);
          if (handled === null) next();
        } catch (e) {
          json(res, 500, { error: String(e) });
        }
      });
    },
  };
}
