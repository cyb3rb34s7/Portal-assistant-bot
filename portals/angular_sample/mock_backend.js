/* Mock backend for the Angular-Material-style sample portal.
 *
 * Mirrors the spirit of sample_portal/mock_backend.js but exposes the
 * endpoints the Frame TV / transfer-artwork flow needs:
 *   - /api/auth/login
 *   - /api/makes
 *   - /api/models?make=&year=
 *   - /api/countries?q=&make=&model=     (server-side search)
 *   - /api/show-data?country=...         (returns ~200 rows)
 *   - /api/transfer
 *
 * Runs as a stand-alone Node HTTP server (the static portal is served
 * from the same process), so a teach/replay test can spin it up via
 *   `node portals/angular_sample/mock_backend.js`
 * and hit http://localhost:5189/transfer-artwork.
 */
const http = require("http");
const fs = require("fs");
const path = require("path");
const url = require("url");

const PORT = parseInt(process.env.PORT || "5189", 10);
const STATIC_DIR = __dirname;

// ----- Seed data ------------------------------------------------------

const MAKES = ["Samsung", "LG", "Sony", "Hisense", "TCL", "Panasonic"];

// (make,year) -> ~6 distinct models. Mix of plausible Frame-TV-ish codes.
const MODEL_MATRIX = {
  "Samsung|2024": [
    "24_BOMRB_8K", "24_KANTM2_8K", "24_KANTM2_4K",
    "24_FRAME_PRO", "24_QLED_OLED", "24_NEO_QLED",
  ],
  "Samsung|2023": [
    "23_FRAME_LS03B", "23_QLED_Q70C", "23_OLED_S90C",
    "23_NEO_QN85C", "23_CRYSTAL_AU8000", "23_LIFESTYLE_THE_SERIF",
  ],
  "Samsung|2022": [
    "22_FRAME_LS03BG", "22_QLED_Q60B", "22_OLED_S95B",
    "22_NEO_QN90B", "22_CRYSTAL_AU7700", "22_LIFESTYLE_THE_SERO",
  ],
  "Samsung|2021": [
    "21_FRAME_LS03A", "21_QLED_Q60A", "21_NEO_QN800A",
    "21_CRYSTAL_AU7100", "21_TERRACE", "21_THE_PREMIERE",
  ],
  "Samsung|2020": [
    "20_FRAME_LS03T", "20_QLED_Q60T", "20_QLED_Q80T",
    "20_CRYSTAL_TU8000", "20_TERRACE", "20_THE_SERIF",
  ],
  "Samsung|2019": [
    "19_FRAME_LS03R", "19_QLED_Q60R", "19_QLED_Q80R",
    "19_RU7100", "19_MUSEM_FRAME", "19_SERIF_LS01R",
  ],
  "Samsung|2018": [
    "18_FRAME_LS03N", "18_BOMRB_8K", "18_KANTM2_8K",
    "18_NU8000", "18_NU7100", "18_OLED_S9",
  ],
  "LG|2024": [
    "24_LG_OLED77G4", "24_LG_OLED55C4", "24_LG_QNED99",
    "24_LG_QNED85", "24_LG_NANO75", "24_LG_UR9000",
  ],
  "LG|2023": [
    "23_LG_OLED77", "23_LG_OLED55C3", "23_LG_QNED99",
    "23_LG_QNED85", "23_LG_NANO75", "23_LG_UR9000",
  ],
  "LG|2022": [
    "22_LG_OLED77G2", "22_LG_OLED55C2", "22_LG_QNED99",
    "22_LG_QNED85", "22_LG_NANO75", "22_LG_UR9000",
  ],
  "Sony|2024": [
    "24_SONY_BRAVIA9", "24_SONY_BRAVIA8", "24_SONY_BRAVIA7",
    "24_SONY_BRAVIA3", "24_SONY_A95L", "24_SONY_X95L",
  ],
  "Sony|2023": [
    "23_SONY_A95L", "23_SONY_X95L", "23_SONY_X90L",
    "23_SONY_X80L", "23_SONY_X75WL", "23_SONY_A80L",
  ],
  "Hisense|2024": [
    "24_HISENSE_U8K", "24_HISENSE_U7K", "24_HISENSE_U6K",
    "24_HISENSE_A7K", "24_HISENSE_A6K", "24_HISENSE_E7K",
  ],
  "TCL|2024": [
    "24_TCL_C805", "24_TCL_C745", "24_TCL_P755",
    "24_TCL_C935", "24_TCL_C655", "24_TCL_S5400",
  ],
  "Panasonic|2024": [
    "24_PANA_MZ2000", "24_PANA_MZ1500", "24_PANA_MX950",
    "24_PANA_MX800", "24_PANA_LX950", "24_PANA_LX800",
  ],
};

function defaultModelsFor(make, year) {
  return [
    String(year).slice(2) + "_" + make.toUpperCase() + "_MODEL_A",
    String(year).slice(2) + "_" + make.toUpperCase() + "_MODEL_B",
    String(year).slice(2) + "_" + make.toUpperCase() + "_MODEL_C",
  ];
}

// ~100 countries
const COUNTRIES = [
  "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Argentina",
  "Armenia", "Australia", "Austria", "Azerbaijan", "Bahrain", "Bangladesh",
  "Belarus", "Belgium", "Bolivia", "Brazil", "Bulgaria", "Cambodia",
  "Cameroon", "Canada", "Chile", "China", "Colombia", "Costa Rica",
  "Croatia", "Cuba", "Cyprus", "Czechia", "Denmark", "Dominican Republic",
  "Ecuador", "Egypt", "El Salvador", "Estonia", "Ethiopia", "Finland",
  "France", "Georgia", "Germany", "Ghana", "Greece", "Guatemala",
  "Honduras", "Hong Kong", "Hungary", "Iceland", "India", "Indonesia",
  "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica",
  "Japan", "Jordan", "Kazakhstan", "Kenya", "Kuwait", "Latvia",
  "Lebanon", "Libya", "Lithuania", "Luxembourg", "Malaysia", "Mexico",
  "Morocco", "Myanmar", "Nepal", "Netherlands", "New Zealand", "Nigeria",
  "Norway", "Oman", "Pakistan", "Panama", "Paraguay", "Peru",
  "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
  "Saudi Arabia", "Senegal", "Serbia", "Singapore", "Slovakia", "Slovenia",
  "South Africa", "South Korea", "Spain", "Sri Lanka", "Sweden", "Switzerland",
  "Taiwan", "Tanzania", "Thailand", "Turkey", "Ukraine", "United Arab Emirates",
  "United Kingdom", "United States", "Uruguay", "Venezuela", "Vietnam", "Zambia",
  "Zimbabwe",
];

function seedAssets() {
  // ~200 plausible "Frame TV" artwork rows
  const out = [];
  const themes = ["Harvest", "Sunset", "Mountain", "Coastal", "Forest",
    "Urban", "Abstract", "Floral", "Portrait", "Still Life"];
  for (let i = 0; i < 200; i++) {
    const id = "SAM-F" + String(1000000 + i).slice(1);
    const theme = themes[i % themes.length];
    out.push({ id, title: theme + " (" + (1950 + (i % 70)) + ")" });
  }
  return out;
}
const ASSETS = seedAssets();

// ----- In-memory state -----
const state = { sessions: new Map() };

// ----- HTTP helpers -----
function json(res, status, body, delay) {
  delay = delay || 0;
  setTimeout(() => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.end(JSON.stringify(body));
  }, delay);
}

function readBody(req) {
  return new Promise((resolve) => {
    let buf = "";
    req.on("data", (c) => (buf += c));
    req.on("end", () => {
      if (!buf) return resolve({});
      try { resolve(JSON.parse(buf)); } catch (e) { resolve({}); }
    });
  });
}

function authedUser(req) {
  const h = req.headers["authorization"] || "";
  if (!h.startsWith("Bearer ")) return null;
  return state.sessions.get(h.slice(7)) || null;
}

function serveStatic(req, res) {
  let p = url.parse(req.url).pathname || "/";
  if (p === "/" || p === "/transfer-artwork") p = "/index.html";
  const filePath = path.join(STATIC_DIR, p);
  // Prevent directory traversal
  if (!filePath.startsWith(STATIC_DIR)) {
    res.statusCode = 403; res.end("forbidden"); return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) { res.statusCode = 404; res.end("not found"); return; }
    const ext = path.extname(filePath).toLowerCase();
    const mime = ({
      ".html": "text/html",
      ".js": "application/javascript",
      ".css": "text/css",
      ".json": "application/json",
    })[ext] || "text/plain";
    res.setHeader("Content-Type", mime);
    res.end(data);
  });
}

// ----- Dispatcher -----
async function dispatch(req, res, parsed) {
  const path = parsed.pathname;
  const method = req.method;
  const qs = parsed.query || {};

  if (method === "OPTIONS") {
    res.statusCode = 200;
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Authorization,Content-Type,Idempotency-Key");
    res.end();
    return true;
  }

  if (path === "/api/auth/login" && method === "POST") {
    const body = await readBody(req);
    if (!body.username) { json(res, 400, { error: "username required" }, 200); return true; }
    const tok = "tok_" + Math.random().toString(36).slice(2, 14);
    state.sessions.set(tok, { username: body.username, issued_at: Date.now() });
    json(res, 200, { token: tok, user: { username: body.username } }, 400);
    return true;
  }

  if (path === "/api/makes" && method === "GET") {
    json(res, 200, MAKES, 200);
    return true;
  }

  if (path === "/api/models" && method === "GET") {
    const make = qs.make || "Samsung";
    const year = qs.year || "2024";
    const key = make + "|" + year;
    const list = MODEL_MATRIX[key] || defaultModelsFor(make, year);
    json(res, 200, list, 300);
    return true;
  }

  if (path === "/api/countries" && method === "GET") {
    const q = (qs.q || "").toString().trim().toLowerCase();
    let rows = COUNTRIES;
    if (q) rows = rows.filter((c) => c.toLowerCase().includes(q));
    // Each row: {id, name}; id is the slug.
    const out = rows.map((c) => ({
      id: c.toLowerCase().replace(/[^a-z]/g, "_"),
      name: c,
    }));
    json(res, 200, out, 300);
    return true;
  }

  if (path === "/api/show-data" && method === "GET") {
    json(res, 200, { rows: ASSETS, total: ASSETS.length }, 800);
    return true;
  }

  if (path === "/api/transfer" && method === "POST") {
    const body = await readBody(req);
    json(res, 200, { ok: true, transferred: (body.ids || []).length }, 400);
    return true;
  }

  return false;
}

// ----- Server -----
const server = http.createServer(async (req, res) => {
  try {
    const parsed = url.parse(req.url, true);
    if (parsed.pathname && parsed.pathname.startsWith("/api/")) {
      const handled = await dispatch(req, res, parsed);
      if (!handled) { res.statusCode = 404; res.end("not found"); }
      return;
    }
    serveStatic(req, res);
  } catch (e) {
    res.statusCode = 500;
    res.end("server error: " + String(e));
  }
});

if (require.main === module) {
  server.listen(PORT, () => {
    process.stdout.write("Angular sample portal listening on http://localhost:" + PORT + "\n");
  });
}

module.exports = { server, MAKES, MODEL_MATRIX, COUNTRIES, ASSETS };
