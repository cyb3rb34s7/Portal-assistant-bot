import { useState } from "react";

export default function ScheduleTab({
  schedules,
  setSchedules,
  markDirty,
  showBanner,
}) {
  const [form, setForm] = useState({
    contentId: "",
    start: "",
    end: "",
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
    if (!form.start || !form.end) {
      setError("Both dates are required");
      return;
    }
    if (form.start > form.end) {
      setError("Start date must precede end date");
      return;
    }
    const next = [
      ...schedules.filter((s) => s.contentId !== form.contentId.trim()),
      {
        contentId: form.contentId.trim(),
        start: form.start,
        end: form.end,
      },
    ];
    setSchedules(next);
    markDirty(false);
    setForm({ contentId: "", start: "", end: "" });
    showBanner(`Schedule saved for ${form.contentId.trim()}`);
  }

  return (
    <div className="tab-panel" data-testid="panel-schedule">
      <form className="curation-form" onSubmit={handleSave}>
        <div className="field">
          <label htmlFor="schedule-content-id">Content ID</label>
          <input
            id="schedule-content-id"
            type="text"
            value={form.contentId}
            onChange={(e) => update("contentId", e.target.value)}
            data-testid="input-schedule-content-id"
          />
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="schedule-start">Start date</label>
            <input
              id="schedule-start"
              type="date"
              value={form.start}
              onChange={(e) => update("start", e.target.value)}
              data-testid="input-schedule-start"
            />
          </div>
          <div className="field">
            <label htmlFor="schedule-end">End date</label>
            <input
              id="schedule-end"
              type="date"
              value={form.end}
              onChange={(e) => update("end", e.target.value)}
              data-testid="input-schedule-end"
            />
          </div>
        </div>
        {error && (
          <div
            className="form-error"
            role="alert"
            data-testid="schedule-error"
          >
            {error}
          </div>
        )}
        <div className="modal-actions">
          <button
            type="submit"
            className="btn primary"
            data-testid="btn-save-schedule"
          >
            Save Schedule
          </button>
        </div>
      </form>

      <h3 className="tab-subtitle">Scheduled items</h3>
      <table className="assets-table" data-testid="schedule-table">
        <thead>
          <tr>
            <th>Content ID</th>
            <th>Start</th>
            <th>End</th>
          </tr>
        </thead>
        <tbody>
          {schedules.length === 0 && (
            <tr>
              <td colSpan={3} className="empty">
                Nothing scheduled yet.
              </td>
            </tr>
          )}
          {schedules.map((s) => (
            <tr
              key={s.contentId}
              data-testid={`schedule-row-${s.contentId}`}
            >
              <td>{s.contentId}</td>
              <td>{s.start}</td>
              <td>{s.end}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
