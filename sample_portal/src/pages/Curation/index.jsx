import { useState } from "react";
import { LAYOUTS, usePortal } from "../../store/PortalStore.jsx";

export default function Curation() {
  const { state, dispatch } = usePortal();

  return (
    <section className="page" data-testid="curation-page">
      <header className="page-header">
        <h1>Curation</h1>
        <p className="muted">
          Pick a layout, fill slots with uploaded contents, attach images, save, and apply.
        </p>
      </header>

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
          <SlotCard key={slot.idx} slot={slot} />
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
            draft.saved ||
            draft.slots.some((s) => !s.content_id || !s.image_uploaded) ||
            !draft.comment.trim()
          }
          onClick={() => dispatch({ type: "SAVE_LAYOUT" })}
        >
          {draft.saved ? "Saved" : "Save Layout"}
        </button>
        <button
          className="btn-secondary"
          data-testid="btn-apply-layout"
          disabled={!draft.saved || draft.applied}
          onClick={() => dispatch({ type: "APPLY_LAYOUT" })}
        >
          {draft.applied ? "Applied" : "Apply Layout"}
        </button>
      </div>

      {draft.saved && (
        <p className="success" data-testid="status-saved">
          Layout saved.
        </p>
      )}
      {draft.applied && (
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
      .map((s) => s.content_id)
  );
  const options = state.contents.filter((c) => !used.has(c.content_id));

  function onPickContent(e) {
    const value = e.target.value || null;
    dispatch({ type: "ASSIGN_SLOT_CONTENT", idx: slot.idx, content_id: value });
  }

  function onPickImage(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    // We don't actually need the bytes; we just record the upload happened.
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
          // Keep the current selection visible even though it'd be filtered.
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
