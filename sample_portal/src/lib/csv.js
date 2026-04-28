// Minimal CSV parser. Handles quoted fields, embedded commas inside quotes,
// and trailing newlines. No streaming; the CSVs we accept are <100 rows.

export function parseCSV(text) {
  const rows = [];
  let cur = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i += 1;
        continue;
      }
      field += c;
      i += 1;
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      i += 1;
      continue;
    }
    if (c === ",") {
      cur.push(field);
      field = "";
      i += 1;
      continue;
    }
    if (c === "\r") {
      i += 1;
      continue;
    }
    if (c === "\n") {
      cur.push(field);
      rows.push(cur);
      cur = [];
      field = "";
      i += 1;
      continue;
    }
    field += c;
    i += 1;
  }
  // last line if no trailing newline
  if (field.length > 0 || cur.length > 0) {
    cur.push(field);
    rows.push(cur);
  }
  return rows.filter((r) => r.length > 0 && r.some((v) => v !== ""));
}

export function csvToObjects(text, expectedHeaders) {
  const rows = parseCSV(text);
  if (rows.length === 0) return { headers: [], objects: [], errors: ["empty CSV"] };
  const headers = rows[0].map((h) => h.trim());
  const errors = [];
  if (expectedHeaders) {
    const missing = expectedHeaders.filter((h) => !headers.includes(h));
    if (missing.length > 0) {
      errors.push(`missing required columns: ${missing.join(", ")}`);
    }
  }
  const objects = rows.slice(1).map((r) => {
    const obj = {};
    headers.forEach((h, idx) => {
      obj[h] = (r[idx] ?? "").trim();
    });
    return obj;
  });
  return { headers, objects, errors };
}
