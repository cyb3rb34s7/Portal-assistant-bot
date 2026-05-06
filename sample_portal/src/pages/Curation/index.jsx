import { useState, useTransition } from "react";
import { LAYOUTS, usePortal } from "../../store/PortalStore.jsx";

export default function Curation() {
  const { state } = usePortal();

  return (
    <section className="page" data-testid="curation-page">
      <header className="page-header">
        <h1>Curation</h1>
        <p className="muted">
          Pick a layout, fill slots with uploaded contents, attach images, save, and apply.
        </p>
      </header>

      <Search />

      {state.contents.length === 0 ? (
        <div className="card" data-testid="no-contents-warning">
          <p>No contents uploaded yet. Go to the Upload tab first.</p>
        </div>
      ) : (
        <>
          <LayoutPicker />
          {state.draftLayout && <LayoutEditor />}
        </>
      )}

      {state.appliedLayouts.length > 0 && <AppliedLayoutsList />}
    </section>
  );
}

// Search exercises two replay scenarios at once:
//   (a) network call with real latency (the runner needs to wait for
//       /api/search to come back before reading the result rows), and
//   (b) variable result count -- partial queries return multiple matches.
//       This is the disambiguation scenario where a recording captured
//       a single specific row but replay is now ambiguous.
//
// Result rows use a useTransition-driven mount stagger so the runner
// also has to handle the "network done but DOM not yet rendered" gap.
function Search() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [renderReady, setRenderReady] = useState(false);
  const [_isPending, startTransition] = useTransition();
  const [error, setError] = useState(null);
  const [lastQuery, setLastQuery] = useState("");

  async function onSearch() {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setRenderReady(false);
    setRows([]);
    setLastQuery(q);
    try {
      const resp = await fetch(
        `/api/search?q=${encodeURIComponent(q.trim())}`,
      );
      const data = await resp.json();
      // Component-render lag: network done but rows are committed via
      // a transition + a small mount-stagger. data-testid="search-results-ready"
      // doesn't appear until the row mount is committed.
      startTransition(() => {
        setRows(data.rows || []);
        setLoading(false);
      });
      // Tiny extra paint window so the runner can't get away with
      // just waiting for networkidle.
      setTimeout(() => setRenderReady(true), 250);
    } catch (e) {
      setError(String(e));
      setLoading(false);
    }
  }

  return (
    <div className="card" data-testid="search-card">
      <h2>Search content</h2>
      <div className="comment-row" style={{ gap: 8 }}>
        <input
          type="text"
          data-testid="input-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="content_id (e.g. A-9001)"
          onKeyDown={(e) => {
            if (e.key === "Enter") onSearch();
          }}
        />
        <button
          className="btn-primary"
          data-testid="btn-search"
          onClick={onSearch}
          disabled={loading || !q.trim()}
        >
          {loading ? "Searching..." : "Search"}
        </button>
      </div>

      {loading && (
        <p className="muted" data-testid="status-searching">
          Searching for {q}...
        </p>
      )}

      {error && (
        <p style={{ color: "var(--err, #dc2626)" }} data-testid="status-search-error">
          {error}
        </p>
      )}

      {!loading && rows.length === 0 && lastQuery && !error && (
        <p
          className="muted"
          data-testid="status-search-empty"
        >
          No rows matched “{lastQuery}”.
        </p>
      )}

      {rows.length > 0 && (
        <div data-testid="search-results">
          <p className="muted">
            {rows.length} result{rows.length === 1 ? "" : "s"} for “{lastQuery}”
          </p>
          <ul className="search-results-list">
            {rows.map((row) => (
              <li
                key={row.content_id}
                className="search-result-row"
                data-testid={`search-row-${row.content_id}`}
              >
                <code>{row.content_id}</code>
                <span> — {row.title}</span>
              </li>
            ))}
          </ul>
          {renderReady && (
            <span data-testid="search-results-ready" style={{ display: "none" }} />
          )}
        </div>
      )}
    </div>
  );
}

function LayoutPicker() {
  const { state, dispatch } = usePortal();
  return (
    <div className="card" data-testid="layout-picker">
      <h2>Choose a layout</h2>
      <div className="layout-options">
        {Object.entries(LAYOUTS).map(([id, info]) => {
          const isActive = state.draftLayout?.layout_id === id;
          return (
            <button
              key={id}
              className={`layout-option ${isActive ? "active" : ""}`}
              data-testid={`layout-option-${id}`}
              onClick={() =>
                dispatch({ type: "SELECT_LAYOUT", layout_id: id, slotCount: info.slotCount })
              }
            >
              <strong>{info.label}</strong>
              <span className="muted">{info.slotCount} slots</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function LayoutEditor() {
  const { state, dispatch } = usePortal();
  const draft = state.draftLayout;

  return (
    <div className="card" data-testid="layout-editor">
      <h2>
        Editing: <code>{draft.layout_id}</code>
      </h2>
      <div className="slots-grid" data-testid={`slots-${draft.layout_id}`}>
        {draft.slots.map((slot) => (
          <SlotCard key={`${draft.layout_id}-${slot.idx}`} slot={slot} />
        ))}
      </div>

      <div className="comment-row">
        <label htmlFor="comment-input">Comment</label>
        <input
          id="comment-input"
          data-testid="input-comment"
          type="text"
          value={draft.comment}
          onChange={(e) => dispatch({ type: "SET_COMMENT", comment: e.target.value })}
          placeholder="e.g. Spring drop hero row"
        />
      </div>

      <div className="action-row">
        <button
          className="btn-primary"
          data-testid="btn-save-layout"
          disabled={
            state.isSaving ||
            draft.saved ||
            draft.slots.some((s) => !s.content_id || !s.image_uploaded) ||
            !draft.comment.trim()
          }
          onClick={async () => {
            dispatch({ type: "SAVE_LAYOUT_START" });
            try {
              // Real network call — the Vite mock middleware adds
              // server-side latency, so CDP networkidle and the runner's
              // network-aware waits will actually wait.
              await fetch("/api/save", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ layout: draft }),
              });
              dispatch({ type: "SAVE_LAYOUT_COMPLETE" });
            } catch {
              dispatch({ type: "SAVE_LAYOUT_COMPLETE" });
            }
          }}
        >
          {state.isSaving
            ? "Saving..."
            : draft.saved
            ? "Saved"
            : "Save Layout"}
        </button>
        <button
          className="btn-secondary"
          data-testid="btn-apply-layout"
          disabled={state.isApplying || !draft.saved || draft.applied}
          onClick={async () => {
            dispatch({ type: "APPLY_LAYOUT_START" });
            try {
              await fetch("/api/apply", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ layout: draft }),
              });
              dispatch({ type: "APPLY_LAYOUT_COMPLETE" });
            } catch {
              dispatch({ type: "APPLY_LAYOUT_COMPLETE" });
            }
          }}
        >
          {state.isApplying
            ? "Applying..."
            : draft.applied
            ? "Applied"
            : "Apply Layout"}
        </button>
      </div>

      {state.isSaving && (
        <p className="muted" data-testid="status-saving">
          Saving layout...
        </p>
      )}
      {state.isApplying && (
        <p className="muted" data-testid="status-applying">
          Applying layout...
        </p>
      )}
      {!state.isSaving && draft.saved && (
        <p className="success" data-testid="status-saved">
          Layout saved.
        </p>
      )}
      {!state.isApplying && draft.applied && (
        <p className="success" data-testid="status-applied">
          Layout applied.
        </p>
      )}
    </div>
  );
}

function SlotCard({ slot }) {
  const { state, dispatch } = usePortal();
  const used = new Set(
    state.draftLayout.slots
      .filter((s) => s.idx !== slot.idx && s.content_id)
      .map((s) => s.content_id),
  );
  const options = state.contents.filter((c) => !used.has(c.content_id));

  function onPickContent(e) {
    const value = e.target.value || null;
    dispatch({ type: "ASSIGN_SLOT_CONTENT", idx: slot.idx, content_id: value });
  }

  function onPickImage(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    dispatch({ type: "UPLOAD_SLOT_IMAGE", idx: slot.idx });
  }

  return (
    <div className="slot-card" data-testid={`slot-${slot.idx}`}>
      <div className="slot-header">Slot {slot.idx}</div>

      <label htmlFor={`slot-${slot.idx}-content`}>Content</label>
      <select
        id={`slot-${slot.idx}-content`}
        data-testid={`slot-${slot.idx}-content-select`}
        value={slot.content_id ?? ""}
        onChange={onPickContent}
      >
        <option value="">— pick content —</option>
        {slot.content_id && !options.find((o) => o.content_id === slot.content_id) && (
          <option value={slot.content_id}>{slot.content_id}</option>
        )}
        {options.map((c) => (
          <option key={c.content_id} value={c.content_id}>
            {c.content_id} — {c.title}
          </option>
        ))}
      </select>

      <label htmlFor={`slot-${slot.idx}-image`} className="image-label">
        Image
      </label>
      <input
        id={`slot-${slot.idx}-image`}
        data-testid={`slot-${slot.idx}-image-input`}
        type="file"
        accept="image/*"
        disabled={!slot.content_id}
        onChange={onPickImage}
      />
      {slot.image_uploaded && (
        <span className="ok" data-testid={`slot-${slot.idx}-image-ok`}>
          uploaded
        </span>
      )}
    </div>
  );
}

function AppliedLayoutsList() {
  const { state } = usePortal();
  return (
    <div className="card" data-testid="applied-layouts">
      <h2>Applied layouts ({state.appliedLayouts.length})</h2>
      <ul>
        {state.appliedLayouts.map((l, i) => (
          <li key={i} data-testid={`applied-layout-${i}`}>
            <code>{l.layout_id}</code> — {l.slots.length} slots — “{l.comment}”
          </li>
        ))}
      </ul>
    </div>
  );
}
