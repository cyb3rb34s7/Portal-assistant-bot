import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api.js";

/* Catalog page -- the "find an asset and open it" entry point.
 *
 * Has a real search (network round-trip on submit) and a category
 * filter that re-queries server-side. The search is intentionally
 * substring-like so "A-90" returns 3 rows -- exercises the multi-
 * result disambiguation scenario from the operator's real portal. */
export default function Catalog() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [categories, setCategories] = useState([]);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [lastQuery, setLastQuery] = useState("");
  const [hasSearched, setHasSearched] = useState(false);

  useEffect(() => {
    api
      .get("/api/categories")
      .then(setCategories)
      .catch((e) => setError(String(e)));
  }, []);

  async function onSearch(e) {
    if (e) e.preventDefault();
    setLoading(true);
    setError(null);
    setHasSearched(true);
    setLastQuery(q);
    try {
      const u = new URLSearchParams();
      if (q.trim()) u.set("q", q.trim());
      if (category) u.set("category", category);
      const r = await api.get(`/api/catalog/assets?${u.toString()}`);
      setRows(r.rows || []);
    } catch (err) {
      setError(String(err.message || err));
      setRows([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="page" data-testid="catalog-page">
      <header className="page-header">
        <h1>Catalog</h1>
        <p className="muted">Search for an asset to edit or schedule.</p>
      </header>

      <form
        className="card"
        data-testid="catalog-search-form"
        onSubmit={onSearch}
      >
        <div className="row">
          <input
            type="text"
            data-testid="input-catalog-search"
            placeholder="Asset ID or title..."
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            data-testid="select-catalog-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="">All categories</option>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <button
            type="submit"
            className="btn-primary"
            data-testid="btn-catalog-search"
            disabled={loading}
          >
            {loading ? "Searching..." : "Search"}
          </button>
        </div>
        {loading && (
          <p className="muted" data-testid="status-catalog-searching">
            Searching the catalog...
          </p>
        )}
        {error && (
          <p className="error" data-testid="status-catalog-error">
            {error}
          </p>
        )}
      </form>

      {hasSearched && !loading && rows.length === 0 && !error && (
        <div className="card" data-testid="catalog-empty">
          No assets matched “{lastQuery}”.
        </div>
      )}

      {rows.length > 0 && (
        <div
          className="card"
          data-testid="catalog-results"
        >
          <h2>
            {rows.length} result{rows.length === 1 ? "" : "s"}
          </h2>
          <table className="catalog-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Title</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  data-testid={`catalog-row-${r.id}`}
                  data-status={r.status}
                >
                  <td>
                    <code>{r.id}</code>
                  </td>
                  <td>{r.title}</td>
                  <td>
                    <span className={`status-pill status-${r.status}`}>
                      {r.status}
                    </span>
                  </td>
                  <td>
                    <button
                      className="btn"
                      data-testid={`btn-open-${r.id}`}
                      onClick={() => navigate(`/asset/${r.id}`)}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
