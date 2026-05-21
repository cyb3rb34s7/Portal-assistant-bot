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
    // WI-28: priority slider 0-100. Drives the publish queue weighting.
    priority: 50,
    // WI-39: contenteditable rich-text description. textContent is
    // tracked here; the editor renders from asset.description_html on
    // mount (uncontrolled afterward to avoid React caret-jump bugs).
    description: "",
  });

  // WI-33: accordion expand-state for the advanced-settings panel.
  // The grabber records aria-expanded transitions so the annotator can
  // emit a toggle_state step whose target is the DESIRED state, not a
  // blind click count.
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // WI-41: global Ctrl+S / Cmd+S save shortcut. Listens at document
  // level when the asset detail page is mounted; releases on
  // unmount. Mirrors the Save button click so the grabber's
  // shortcut detector records ONE shortcut step that replays via
  // page.keyboard.press("Control+S") without typing 'S' into the
  // focused field.
  useEffect(() => {
    function onKeyDown(e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
        e.preventDefault();
        // Use a ref-style closure: schedule save on the next tick so
        // any pending input commits first.
        setTimeout(() => {
          const saveBtn = document.querySelector(
            '[data-testid="btn-save"]'
          );
          if (saveBtn && !saveBtn.disabled) saveBtn.click();
        }, 0);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // WI-30: ordered-list drag-and-drop for the asset's "related items"
  // ranking. The operator drags items between two columns ("included"
  // vs "excluded") and the grabber captures the dragstart/dragover/
  // drop sequence so the annotator can collapse into a drag_drop step.
  const [relatedIncluded, setRelatedIncluded] = useState([
    "promo-1", "promo-2", "promo-3",
  ]);
  const [relatedExcluded, setRelatedExcluded] = useState([
    "promo-4", "promo-5",
  ]);
  const [dragItem, setDragItem] = useState(null);

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
          priority: a.priority != null ? a.priority : 50,
          description: a.description || "",
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

        {/* WI-39: rich-text contenteditable description editor. Plain
            contenteditable (no framework) -- the grabber's WI-39
            contenteditable burst capture should collapse typing into
            one rich_text_set step. The element root carries
            contenteditable=true; React doesn't control its content
            (we read from it on save via the ref). */}
        <label htmlFor="asset-description">Description</label>
        <div
          id="asset-description"
          data-testid="input-description"
          contentEditable={isDraft}
          suppressContentEditableWarning={true}
          style={{
            border: "1px solid var(--border, #ccc)",
            borderRadius: 4,
            padding: 8,
            minHeight: 60,
            background: isDraft ? "white" : "#f6f6f6",
          }}
          onInput={(e) => {
            // Reflect editor textContent into form.description so save
            // captures it. innerHTML is preserved in the DOM verbatim.
            setForm({
              ...form,
              description: e.currentTarget.textContent || "",
            });
          }}
          dangerouslySetInnerHTML={
            asset && asset.description_html != null
              ? { __html: asset.description_html }
              : undefined
          }
        />

        {/* WI-28: priority slider. Range 0-100, step 5. The grabber's
            WI-28 input listener captures the drag burst; the annotator
            collapses it into one slider_set step. */}
        <label htmlFor="asset-priority">
          Priority: <span data-testid="priority-value">{form.priority}</span>
        </label>
        <input
          id="asset-priority"
          type="range"
          min={0}
          max={100}
          step={5}
          data-testid="input-priority"
          value={form.priority}
          onChange={(e) =>
            setForm({ ...form, priority: Number(e.target.value) })
          }
          disabled={!isDraft}
        />

        {/* WI-33: advanced-settings accordion. aria-expanded transitions
            between true / false on header click. The grabber's WI-03
            captures aria-expanded; the annotator emits a toggle_state
            step whose target is the desired state, not a blind click. */}
        <div className="accordion" data-testid="accordion-advanced">
          <button
            type="button"
            className="btn"
            data-testid="accordion-advanced-toggle"
            aria-expanded={advancedOpen}
            aria-controls="accordion-advanced-panel"
            onClick={() => setAdvancedOpen((v) => !v)}
          >
            {advancedOpen ? "Hide" : "Show"} advanced settings
          </button>
          {advancedOpen && (
            <div
              id="accordion-advanced-panel"
              data-testid="accordion-advanced-panel"
              role="region"
              aria-labelledby="accordion-advanced-toggle"
            >
              <p className="muted">
                Advanced settings revealed. Internal flags would appear here
                in production.
              </p>
            </div>
          )}
        </div>

        {/* WI-30: drag/drop ordering between included and excluded
            related items. The grabber hooks dragstart/dragover/drop
            on these zones; the annotator collapses the burst into a
            drag_drop step. */}
        <div className="drag-zones" data-testid="related-items-block">
          <div
            className="drag-zone"
            data-testid="drag-zone-included"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const item = e.dataTransfer.getData("text/plain") || dragItem;
              if (!item) return;
              if (relatedExcluded.includes(item)) {
                setRelatedExcluded(relatedExcluded.filter((x) => x !== item));
                if (!relatedIncluded.includes(item)) {
                  setRelatedIncluded([...relatedIncluded, item]);
                }
              }
              setDragItem(null);
            }}
          >
            <h4>Included</h4>
            <ul>
              {relatedIncluded.map((it) => (
                <li
                  key={it}
                  draggable
                  data-testid={`drag-item-${it}`}
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/plain", it);
                    e.dataTransfer.effectAllowed = "move";
                    setDragItem(it);
                  }}
                >
                  {it}
                </li>
              ))}
            </ul>
          </div>
          <div
            className="drag-zone"
            data-testid="drag-zone-excluded"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const item = e.dataTransfer.getData("text/plain") || dragItem;
              if (!item) return;
              if (relatedIncluded.includes(item)) {
                setRelatedIncluded(relatedIncluded.filter((x) => x !== item));
                if (!relatedExcluded.includes(item)) {
                  setRelatedExcluded([...relatedExcluded, item]);
                }
              }
              setDragItem(null);
            }}
          >
            <h4>Excluded</h4>
            <ul>
              {relatedExcluded.map((it) => (
                <li
                  key={it}
                  draggable
                  data-testid={`drag-item-${it}`}
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/plain", it);
                    e.dataTransfer.effectAllowed = "move";
                    setDragItem(it);
                  }}
                >
                  {it}
                </li>
              ))}
            </ul>
          </div>
        </div>

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
