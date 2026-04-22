import { useMemo, useState } from "react";

const initialAssets = [
  {
    id: "A-1001",
    title: "Morning News Highlights",
    type: "video",
    status: "published",
    updated: "2026-04-01",
  },
  {
    id: "A-1002",
    title: "Weekly Sports Recap",
    type: "video",
    status: "draft",
    updated: "2026-04-10",
  },
  {
    id: "A-1003",
    title: "Cooking Show Promo",
    type: "image",
    status: "published",
    updated: "2026-04-15",
  },
];

export default function MediaAssets() {
  const [assets, setAssets] = useState(initialAssets);
  const [query, setQuery] = useState("");
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [banner, setBanner] = useState(null);
  const [form, setForm] = useState({
    id: "",
    title: "",
    type: "video",
    description: "",
  });
  const [formError, setFormError] = useState(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return assets;
    return assets.filter(
      (a) =>
        a.id.toLowerCase().includes(q) ||
        a.title.toLowerCase().includes(q)
    );
  }, [assets, query]);

  function openAddModal() {
    setForm({ id: "", title: "", type: "video", description: "" });
    setFormError(null);
    setIsModalOpen(true);
  }

  function closeModal() {
    setIsModalOpen(false);
  }

  function handleSubmit(e) {
    e.preventDefault();
    if (!form.id.trim() || !form.title.trim()) {
      setFormError("ID and Title are required.");
      return;
    }
    if (assets.some((a) => a.id === form.id.trim())) {
      setFormError(`Asset with id ${form.id} already exists.`);
      return;
    }
    const newAsset = {
      id: form.id.trim(),
      title: form.title.trim(),
      type: form.type,
      status: "draft",
      updated: new Date().toISOString().slice(0, 10),
    };
    setAssets((prev) => [...prev, newAsset]);
    setIsModalOpen(false);
    showBanner(`Asset ${newAsset.id} created successfully.`, "success");
  }

  function handleDelete(id) {
    setAssets((prev) => prev.filter((a) => a.id !== id));
    showBanner(`Asset ${id} deleted.`, "success");
  }

  function showBanner(message, kind) {
    setBanner({ message, kind });
    setTimeout(() => setBanner(null), 4000);
  }

  return (
    <section className="page" data-testid="page-media-assets">
      <header className="page-header">
        <h1>Media Assets</h1>
        <button
          className="btn primary"
          onClick={openAddModal}
          data-testid="btn-add-asset"
        >
          Add Asset
        </button>
      </header>

      {banner && (
        <div
          className={`banner ${banner.kind}`}
          role="status"
          data-testid="status-banner"
        >
          {banner.message}
        </div>
      )}

      <div className="toolbar">
        <label className="search-label" htmlFor="asset-search">
          Search
        </label>
        <input
          id="asset-search"
          type="text"
          className="search-input"
          placeholder="Search by id or title"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="asset-search"
        />
      </div>

      <table className="assets-table" data-testid="assets-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Title</th>
            <th>Type</th>
            <th>Status</th>
            <th>Updated</th>
            <th aria-label="Actions"></th>
          </tr>
        </thead>
        <tbody>
          {filtered.length === 0 && (
            <tr>
              <td colSpan={6} className="empty">
                No assets found.
              </td>
            </tr>
          )}
          {filtered.map((asset) => (
            <tr key={asset.id} data-testid={`asset-row-${asset.id}`}>
              <td data-testid={`asset-id-${asset.id}`}>{asset.id}</td>
              <td>{asset.title}</td>
              <td>{asset.type}</td>
              <td>
                <span className={`badge ${asset.status}`}>{asset.status}</span>
              </td>
              <td>{asset.updated}</td>
              <td className="row-actions">
                <button
                  className="btn danger"
                  onClick={() => handleDelete(asset.id)}
                  data-testid={`btn-delete-${asset.id}`}
                  aria-label={`Delete asset ${asset.id}`}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {isModalOpen && (
        <div
          className="modal-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="add-asset-title"
          data-testid="add-asset-modal"
        >
          <div className="modal">
            <h2 id="add-asset-title">Add Asset</h2>
            <form onSubmit={handleSubmit} noValidate>
              <div className="field">
                <label htmlFor="asset-id">ID</label>
                <input
                  id="asset-id"
                  type="text"
                  value={form.id}
                  onChange={(e) => setForm({ ...form, id: e.target.value })}
                  data-testid="input-asset-id"
                />
              </div>
              <div className="field">
                <label htmlFor="asset-title">Title</label>
                <input
                  id="asset-title"
                  type="text"
                  value={form.title}
                  onChange={(e) =>
                    setForm({ ...form, title: e.target.value })
                  }
                  data-testid="input-asset-title"
                />
              </div>
              <div className="field">
                <label htmlFor="asset-type">Type</label>
                <select
                  id="asset-type"
                  value={form.type}
                  onChange={(e) => setForm({ ...form, type: e.target.value })}
                  data-testid="input-asset-type"
                >
                  <option value="video">video</option>
                  <option value="image">image</option>
                  <option value="audio">audio</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="asset-description">Description</label>
                <textarea
                  id="asset-description"
                  rows={3}
                  value={form.description}
                  onChange={(e) =>
                    setForm({ ...form, description: e.target.value })
                  }
                  data-testid="input-asset-description"
                />
              </div>
              {formError && (
                <div
                  className="form-error"
                  role="alert"
                  data-testid="form-error"
                >
                  {formError}
                </div>
              )}
              <div className="modal-actions">
                <button
                  type="button"
                  className="btn"
                  onClick={closeModal}
                  data-testid="btn-cancel"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn primary"
                  data-testid="btn-save-asset"
                >
                  Save
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}
