import { useRef, useState } from "react";
import { csvToObjects } from "../lib/csv";
import { usePortal } from "../store/PortalStore.jsx";

const REQUIRED = ["content_id", "title", "image_path", "category", "release_date"];

export default function Upload() {
  const { state, dispatch } = usePortal();
  const fileRef = useRef(null);
  const [errors, setErrors] = useState([]);
  const [filename, setFilename] = useState("");

  async function onFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setFilename(file.name);
    const text = await file.text();
    const { objects, errors: csvErrors } = csvToObjects(text, REQUIRED);
    if (csvErrors.length > 0) {
      setErrors(csvErrors);
      return;
    }
    const bad = objects.filter((o) => !o.content_id);
    if (bad.length > 0) {
      setErrors([`${bad.length} row(s) missing content_id`]);
      return;
    }
    setErrors([]);
    dispatch({ type: "UPLOAD_CONTENTS", contents: objects });
  }

  return (
    <section className="page" data-testid="upload-page">
      <header className="page-header">
        <h1>Upload Contents</h1>
        <p className="muted">
          Upload a CSV with columns:{" "}
          <code>content_id, title, image_path, category, release_date</code>.
        </p>
      </header>

      <div className="card" data-testid="csv-upload-card">
        <label htmlFor="csv-input" className="btn-primary">
          Choose CSV
        </label>
        <input
          id="csv-input"
          data-testid="input-csv-file"
          ref={fileRef}
          type="file"
          accept=".csv,text/csv"
          onChange={onFile}
          style={{ display: "none" }}
        />
        {filename && (
          <span className="muted" data-testid="csv-filename">
            {" "}
            {filename}
          </span>
        )}
        {errors.length > 0 && (
          <ul className="errors" data-testid="csv-errors">
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        )}
      </div>

      <div className="card" data-testid="contents-table-card">
        <h2>Uploaded Contents ({state.contents.length})</h2>
        {state.contents.length === 0 ? (
          <p className="muted">No contents uploaded yet.</p>
        ) : (
          <table className="data-table" data-testid="contents-table">
            <thead>
              <tr>
                <th>Content ID</th>
                <th>Title</th>
                <th>Image Path</th>
                <th>Category</th>
                <th>Release Date</th>
              </tr>
            </thead>
            <tbody>
              {state.contents.map((c) => (
                <tr key={c.content_id} data-testid={`row-${c.content_id}`}>
                  <td>{c.content_id}</td>
                  <td>{c.title}</td>
                  <td className="path">{c.image_path}</td>
                  <td>{c.category}</td>
                  <td>{c.release_date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
