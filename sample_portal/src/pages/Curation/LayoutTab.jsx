import { useState } from "react";

export default function LayoutTab({ rows, setRows, markDirty, showBanner }) {
  const [form, setForm] = useState({
    contentId: "",
    row: 1,
    position: 1,
  });
  const [error, setError] = useState(null);

  function update(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
    markDirty(true);
    setError(null);
  }

  function handleSave(e) {
    e.preventDefault();
    if (!form.contentId.trim()) {
      setError("Content ID is required");
      return;
    }
    const parsedRow = Number(form.row);
    const parsedPos = Number(form.position);
    if (!parsedRow || !parsedPos) {
      setError("Row and position must be numbers");
      return;
    }
    const next = [
      ...rows.filter((r) => r.contentId !== form.contentId.trim()),
      {
        contentId: form.contentId.trim(),
        row: parsedRow,
        position: parsedPos,
      },
    ];
    setRows(next);
    markDirty(false);
    setForm({ contentId: "", row: 1, position: 1 });
    showBanner(`Layout saved for ${form.contentId.trim()}`);
  }

  return (
    <div className="tab-panel" data-testid="panel-layout">
      <form className="curation-form" onSubmit={handleSave}>
        <div className="field">
          <label htmlFor="layout-content-id">Content ID</label>
          <input
            id="layout-content-id"
            type="text"
            value={form.contentId}
            onChange={(e) => update("contentId", e.target.value)}
            data-testid="input-layout-content-id"
          />
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="layout-row">Row</label>
            <input
              id="layout-row"
              type="number"
              min="1"
              max="20"
              value={form.row}
              onChange={(e) => update("row", e.target.value)}
              data-testid="input-layout-row"
            />
          </div>
          <div className="field">
            <label htmlFor="layout-position">Position</label>
            <input
              id="layout-position"
              type="number"
              min="1"
              max="10"
              value={form.position}
              onChange={(e) => update("position", e.target.value)}
              data-testid="input-layout-position"
            />
          </div>
        </div>
        {error && (
          <div className="form-error" role="alert" data-testid="layout-error">
            {error}
          </div>
        )}
        <div className="modal-actions">
          <button
            type="submit"
            className="btn primary"
            data-testid="btn-save-layout"
          >
            Save Layout
          </button>
        </div>
      </form>

      <h3 className="tab-subtitle">Current layout</h3>
      <table
        className="assets-table"
        data-testid="layout-table"
        aria-label="Current layout assignments"
      >
        <thead>
          <tr>
            <th>Content ID</th>
            <th>Row</th>
            <th>Position</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={3} className="empty">
                No layout assignments yet.
              </td>
            </tr>
          )}
          {rows.map((r) => (
            <tr key={r.contentId} data-testid={`layout-row-${r.contentId}`}>
              <td>{r.contentId}</td>
              <td>{r.row}</td>
              <td>{r.position}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
