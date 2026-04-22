import { useRef, useState } from "react";

export default function ThumbnailsTab({
  thumbnails,
  setThumbnails,
  markDirty,
  showBanner,
}) {
  const [form, setForm] = useState({ contentId: "", fileName: "" });
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);

  function handleFileChange(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (!/\.(jpg|jpeg|png)$/i.test(file.name)) {
      setError("Only JPG or PNG files are accepted");
      return;
    }
    setError(null);
    setForm((prev) => ({ ...prev, fileName: file.name }));
    markDirty(true);
  }

  function triggerUpload() {
    fileInputRef.current?.click();
  }

  function handleSave(e) {
    e.preventDefault();
    if (!form.contentId.trim()) {
      setError("Content ID is required");
      return;
    }
    if (!form.fileName) {
      setError("Please upload a thumbnail first");
      return;
    }
    const next = [
      ...thumbnails.filter((t) => t.contentId !== form.contentId.trim()),
      {
        contentId: form.contentId.trim(),
        fileName: form.fileName,
      },
    ];
    setThumbnails(next);
    markDirty(false);
    setForm({ contentId: "", fileName: "" });
    showBanner(`Thumbnail uploaded for ${form.contentId.trim()}`);
  }

  return (
    <div className="tab-panel" data-testid="panel-thumbnails">
      <form className="curation-form" onSubmit={handleSave}>
        <div className="field">
          <label htmlFor="thumb-content-id">Content ID</label>
          <input
            id="thumb-content-id"
            type="text"
            value={form.contentId}
            onChange={(e) => {
              setForm((prev) => ({ ...prev, contentId: e.target.value }));
              markDirty(true);
              setError(null);
            }}
            data-testid="input-thumb-content-id"
          />
        </div>
        <div className="field">
          <label>Thumbnail file</label>
          <div className="upload-row">
            <button
              type="button"
              className="btn"
              onClick={triggerUpload}
              data-testid="btn-choose-thumb"
            >
              Choose file
            </button>
            <span className="upload-filename" data-testid="thumb-filename">
              {form.fileName || "No file chosen"}
            </span>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/jpeg,image/png"
              style={{ display: "none" }}
              onChange={handleFileChange}
              data-testid="input-thumb-file"
            />
          </div>
        </div>
        {error && (
          <div className="form-error" role="alert" data-testid="thumb-error">
            {error}
          </div>
        )}
        <div className="modal-actions">
          <button
            type="submit"
            className="btn primary"
            data-testid="btn-save-thumb"
          >
            Save Thumbnail
          </button>
        </div>
      </form>

      <h3 className="tab-subtitle">Uploaded thumbnails</h3>
      <table className="assets-table" data-testid="thumbnails-table">
        <thead>
          <tr>
            <th>Content ID</th>
            <th>File</th>
          </tr>
        </thead>
        <tbody>
          {thumbnails.length === 0 && (
            <tr>
              <td colSpan={2} className="empty">
                No thumbnails uploaded.
              </td>
            </tr>
          )}
          {thumbnails.map((t) => (
            <tr
              key={t.contentId}
              data-testid={`thumb-row-${t.contentId}`}
            >
              <td>{t.contentId}</td>
              <td>{t.fileName}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
