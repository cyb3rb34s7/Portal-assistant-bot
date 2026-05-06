import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Mock backend: server-side delays so CDP networkidle and the runner's
// network-aware waits actually have something to wait for. We can't
// just setTimeout in the React handler — that delay is invisible to the
// network layer, so the runner has no signal to key off of.
//
// /api/save        ~ 900ms     ok body
// /api/apply       ~ 1500ms    ok body
// /api/search?q=X  variable    returns rows whose content_id contains X
function mockApiPlugin(catalog) {
  return {
    name: "mock-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url || !req.url.startsWith("/api/")) return next();

        const send = (status, body, delayMs) => {
          setTimeout(() => {
            res.statusCode = status;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify(body));
          }, delayMs);
        };

        if (req.url.startsWith("/api/save")) {
          return send(200, { ok: true, ts: Date.now() }, 900);
        }
        if (req.url.startsWith("/api/apply")) {
          return send(200, { ok: true, ts: Date.now() }, 1500);
        }
        if (req.url.startsWith("/api/search")) {
          const u = new URL(req.url, "http://localhost");
          const q = (u.searchParams.get("q") || "").trim().toUpperCase();
          // Catalog comes from the global store; for the search demo we
          // use a stable seeded set of rows so behavior is reproducible.
          const rows = catalog.filter((r) =>
            r.content_id.toUpperCase().includes(q),
          );
          return send(200, { rows, total: rows.length, q }, 700);
        }
        next();
      });
    },
  };
}

// Stable seed catalog used by /api/search. Includes intentional prefix
// overlaps (A-9001 / A-9002 / A-9003) so a partial query "A-90" returns
// 3 rows -- the multi-result disambiguation scenario.
const SEARCH_CATALOG = [
  { content_id: "A-9001", title: "Spring Hero" },
  { content_id: "A-9002", title: "Spring Backdrop" },
  { content_id: "A-9003", title: "Spring Side" },
  { content_id: "A-1001", title: "Winter Hero" },
  { content_id: "B-2001", title: "Sale Banner" },
  { content_id: "B-2002", title: "Sale Footer" },
  { content_id: "C-3001", title: "Promo Tile" },
];

export default defineConfig({
  plugins: [react(), mockApiPlugin(SEARCH_CATALOG)],
  server: {
    port: 5188,
    strictPort: true,
  },
});
