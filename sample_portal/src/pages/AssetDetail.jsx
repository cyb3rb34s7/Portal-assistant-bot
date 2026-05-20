import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, newIdempotencyKey } from "../lib/api.js";
import MultiSelect from "../components/MultiSelect.jsx";

/* AssetDetail -- the heart of the workflow.
 *
 * Loads the asset, lets the operator edit it (title, multi-select
 * categories + tags, cascading region->market->language, publish
 * date, comment), then submits the workflow transitions (submit for
 * review -> approve/reject -> publish).
 *
 * Cascading dropdowns are real network calls: picking Region fires
 * /api/markets?region=XX (~2s), and picking Market fires
 * /api/languages?market=XX (~1.5s). The replay-time wait logic has
 * to actually wait for these or the next dropdown sees stale options.
 */
export default function AssetDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [asset, setAsset] = useState(null);
  const [categories, setCategories] = useState([]);
  const [tags, setTags] = useState([]);
  const [regions, setRegions] = useState([]);
  const [markets, setMarkets] = useState([]);
  const [languages, setLanguages] = useState([]);
  const [loadingMarkets, setLoadingMarkets] = useState(false);
  const [loadingLanguages, setLoadingLanguages] = useState(false);

  const [saving, setSaving] = useState(false);
  const [transitioning, setTransitioning] = useState(false);
  const [error, setError] = useState(null);

  // Editable form state (mirrors the asset, plus local-only fields).
  const [form, setForm] = useState({
    title: "",
    categories: [],
    tags: [],
    region: "",
    market: "",
    language: "",
    publish_at: "",
    comment: "",
  });

  // ---- Initial loads ----
  useEffect(() => {
    api.get("/api/categories").then(setCategories);
    api.get("/api/tags").then(setTags);
    api.get("/api/regions").then(setRegions);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    api
      .get(`/api/assets/${encodeURIComponent(id)}`)
      .then((a) => {
        if (cancelled) return;
        setAsset(a);
        setForm({
          title: a.title || "",
          categories: a.categories || [],
          tags: a.tags || [],
          region: a.region || "",
          market: a.market || "",
          language: a.language || "",
          publish_at: a.publish_at || "",
          comment: a.comment || "",
        });
      })
      .catch((e) => !cancelled && setError(String(e.message || e)));
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Cascading: when region changes, refetch markets, clear market+language.
  useEffect(() => {
    if (!form.region) {
      setMarkets([]);
      return;
    }
    setLoadingMarkets(true);
    api
      .get(`/api/markets?region=${encodeURIComponent(form.region)}`)
      .then((r) => setMarkets(r))
      .catch(() => setMarkets([]))
      .finally(() => setLoadingMarkets(false));
  }, [form.region]);

  useEffect(() => {
    if (!form.market) {
      setLanguages([]);
      return;
    }
    setLoadingLanguages(true);
    api
      .get(`/api/languages?market=${encodeURIComponent(form.market)}`)
      .then((r) => setLanguages(r))
      .catch(() => setLanguages([]))
      .finally(() => setLoadingLanguages(false));
  }, [form.market]);

  // ---- Persist ----
  async function onSave() {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.patch(
        `/api/assets/${encodeURIComponent(id)}`,
        form,
        { idempotencyKey: newIdempotencyKey() },
      );
      setAsset(updated);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  async function transition(verb) {
    setTransitioning(true);
    setError(null);
    try {
      const updated = await api.post(
        `/api/assets/${encodeURIComponent(id)}/${verb}`,
        {},
        { idempotencyKey: newIdempotencyKey() },
      );
      setAsset(updated);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setTransitioning(false);
    }
  }

  if (error && !asset) {
    return (
      <section className="page" data-testid="asset-page">
        <p className="error" data-testid="status-asset-error">
          {error}
        </p>
        <button className="btn" onClick={() => navigate("/catalog")}>
          Back to catalog
        </button>
      </section>
    );
  }
  if (!asset) {
    return (
      <section className="page" data-testid="asset-page">
        <p className="muted" data-testid="status-asset-loading">
          Loading asset {id}...
        </p>
      </section>
    );
  }

  const isDraft = asset.status === "draft";
  const isInReview = asset.status === "in_review";
  const isApproved = asset.status === "approved";

  return (
    <section className="page" data-testid="asset-page">
      <header className="page-header">
        <h1>
          Asset <code>{asset.id}</code>
        </h1>
        <p className="muted">
          Status:{" "}
          <span
            className={`status-pill status-${asset.status}`}
            data-testid="asset-status"
          >
            {asset.status}
          </span>
        </p>
      </header>

      <div className="card" data-testid="asset-edit-form">
        <label htmlFor="asset-title">Title</label>
        <input
          id="asset-title"
          type="text"
          data-testid="input-asset-title"
          value={form.title}
          onChange={(e) => setForm({ ...form, title: e.target.value })}
          disabled={!isDraft}
        />

        <MultiSelect
          label="Categories"
          testId="multiselect-categories"
          options={categories}
          value={form.categories}
          onChange={(v) => setForm({ ...form, categories: v })}
          placeholder="Pick categories..."
          disabled={!isDraft}
        />

        <MultiSelect
          label="Tags"
          testId="multiselect-tags"
          options={tags}
          value={form.tags}
          onChange={(v) => setForm({ ...form, tags: v })}
          placeholder="Pick tags..."
          disabled={!isDraft}
        />

        <div className="cascade-row">
          <div>
            <label htmlFor="asset-region">Region</label>
            <select
              id="asset-region"
              data-testid="select-region"
              value={form.region}
              onChange={(e) =>
                // When region changes, clear market + language. The
                // user has to pick fresh dependent values.
                setForm({
                  ...form,
                  region: e.target.value,
                  market: "",
                  language: "",
                })
              }
              disabled={!isDraft}
            >
              <option value="">—</option>
              {regions.map((r) => (
                <option key={r.code} value={r.code}>
                  {r.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="asset-market">Market</label>
            <select
              id="asset-market"
              data-testid="select-market"
              value={form.market}
              onChange={(e) =>
                setForm({
                  ...form,
                  market: e.target.value,
                  language: "",
                })
              }
              disabled={!isDraft || !form.region || loadingMarkets}
            >
              <option value="">
                {loadingMarkets
                  ? "Loading markets..."
                  : form.region
                  ? "—"
                  : "(pick region first)"}
              </option>
              {markets.map((m) => (
                <option key={m.code} value={m.code}>
                  {m.name}
                </option>
              ))}
            </select>
            {loadingMarkets && (
              <span
                className="muted"
                data-testid="status-loading-markets"
              >
                loading...
              </span>
            )}
          </div>

          <div>
            <label htmlFor="asset-language">Language</label>
            <select
              id="asset-language"
              data-testid="select-language"
              value={form.language}
              onChange={(e) => setForm({ ...form, language: e.target.value })}
              disabled={!isDraft || !form.market || loadingLanguages}
            >
              <option value="">
                {loadingLanguages
                  ? "Loading languages..."
                  : form.market
                  ? "—"
                  : "(pick market first)"}
              </option>
              {languages.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.name}
                </option>
              ))}
            </select>
            {loadingLanguages && (
              <span
                className="muted"
                data-testid="status-loading-languages"
              >
                loading...
              </span>
            )}
          </div>
        </div>

        <label htmlFor="asset-publish-at">Publish date</label>
        <input
          id="asset-publish-at"
          type="date"
          data-testid="input-publish-at"
          value={form.publish_at || ""}
          onChange={(e) => setForm({ ...form, publish_at: e.target.value })}
          disabled={!isDraft}
        />

        <label htmlFor="asset-comment">Comment</label>
        <textarea
          id="asset-comment"
          data-testid="input-comment"
          rows={2}
          value={form.comment}
          onChange={(e) => setForm({ ...form, comment: e.target.value })}
          disabled={!isDraft}
        />

        <div className="action-row">
          <button
            className="btn-primary"
            data-testid="btn-save"
            onClick={onSave}
            disabled={!isDraft || saving}
          >
            {saving ? "Saving..." : "Save"}
          </button>
          <button
            className="btn"
            data-testid="btn-submit-review"
            onClick={() => transition("submit_review")}
            disabled={!isDraft || transitioning}
          >
            {transitioning ? "Submitting..." : "Submit for review"}
          </button>
          <button
            className="btn"
            data-testid="btn-approve"
            onClick={() => transition("approve")}
            disabled={!isInReview || transitioning}
          >
            {transitioning ? "Approving..." : "Approve"}
          </button>
          <button
            className="btn"
            data-testid="btn-reject"
            onClick={() => transition("reject")}
            disabled={!isInReview || transitioning}
          >
            {transitioning ? "Rejecting..." : "Reject"}
          </button>
          <button
            className="btn-primary"
            data-testid="btn-publish"
            onClick={() => transition("publish")}
            disabled={!isApproved || transitioning}
          >
            {transitioning ? "Publishing..." : "Publish"}
          </button>
        </div>

        {saving && (
          <p className="muted" data-testid="status-saving">
            Saving asset...
          </p>
        )}
        {transitioning && (
          <p className="muted" data-testid="status-transitioning">
            Updating workflow state...
          </p>
        )}
        {error && (
          <p className="error" data-testid="status-asset-action-error">
            {error}
          </p>
        )}
      </div>

      {(asset.workflow_history || []).length > 0 && (
        <div className="card" data-testid="workflow-history">
          <h2>Workflow history</h2>
          <ol>
            {asset.workflow_history.map((h, i) => (
              <li key={i}>
                <code>{h.from}</code> → <code>{h.to}</code> by{" "}
                <strong>{h.by}</strong>
                {h.reason ? ` — ${h.reason}` : ""}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
