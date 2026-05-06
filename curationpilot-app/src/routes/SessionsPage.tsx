import { useEffect, useState } from "react";
import { bridge } from "../host";
import type { SessionSummary } from "../protocol/types";

export default function SessionsPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  useEffect(() => {
    bridge
      .listSessions()
      .then((s) => {
        setSessions(s);
        setLoading(false);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  async function openReport(id: string) {
    setOpenId(id);
    setReport(null);
    try {
      const md = await bridge.getSessionReport(id);
      setReport(md);
    } catch (e) {
      setReport(`(no report.md — ${(e as Error).message})`);
    }
  }

  return (
    <div style={{ maxWidth: 1000 }}>
      <h1
        style={{
          color: "var(--text-bright)",
          fontSize: 24,
          margin: "0 0 6px",
          fontWeight: 500,
        }}
      >
        Sessions
      </h1>
      <p className="muted" style={{ marginBottom: 24 }}>
        Past teach + replay runs. Each row is a directory under{" "}
        <code>sessions/&lt;id&gt;/</code>.
      </p>

      {loading && <div className="muted">Loading...</div>}
      {error && (
        <pre style={{ color: "var(--danger)", fontSize: 12 }}>{error}</pre>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: openId ? "320px 1fr" : "1fr",
          gap: 14,
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {sessions.length === 0 && !loading && (
            <div className="muted">No sessions yet.</div>
          )}
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => openReport(s.id)}
              className="surface-card"
              style={{
                padding: 12,
                background: openId === s.id ? "rgba(106,163,255,0.06)" : undefined,
                borderColor:
                  openId === s.id ? "rgba(106,163,255,0.4)" : undefined,
                textAlign: "left",
                cursor: "pointer",
                border: "1px solid var(--border)",
              }}
            >
              <div
                style={{
                  fontSize: 13,
                  color: "var(--text-bright)",
                  fontWeight: 500,
                }}
              >
                {s.skill_name || "(unnamed)"}
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                <code>{s.id}</code>
              </div>
              <div
                style={{
                  display: "flex",
                  gap: 6,
                  marginTop: 6,
                  flexWrap: "wrap",
                }}
              >
                {s.has_report && <span className="tag tag-ok">report</span>}
                {s.event_count != null && (
                  <span className="tag">{s.event_count} events</span>
                )}
                {s.screenshot_count > 0 && (
                  <span className="tag">{s.screenshot_count} shots</span>
                )}
              </div>
            </button>
          ))}
        </div>

        {openId && (
          <div className="surface-card" style={{ padding: 18 }}>
            <div
              style={{
                color: "var(--text-bright)",
                fontWeight: 500,
                marginBottom: 8,
              }}
            >
              {openId}
            </div>
            <pre
              style={{
                whiteSpace: "pre-wrap",
                fontSize: 13,
                lineHeight: 1.6,
                background: "var(--bg)",
                border: "none",
                padding: 0,
                color: "var(--text)",
              }}
            >
              {report ?? "Loading..."}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
