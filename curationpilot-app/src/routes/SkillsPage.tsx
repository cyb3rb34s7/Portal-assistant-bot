import { useEffect, useState } from "react";
import { bridge } from "../host";
import type { SkillSummary } from "../protocol/types";

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    bridge
      .listSkills()
      .then((s) => {
        setSkills(s);
        setLoading(false);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  return (
    <div style={{ maxWidth: 920 }}>
      <h1
        style={{
          color: "var(--text-bright)",
          fontSize: 24,
          margin: "0 0 6px",
          fontWeight: 500,
        }}
      >
        Skills
      </h1>
      <p className="muted" style={{ marginBottom: 24 }}>
        Recorded workflows, parametrised, ready to replay. The badge after
        the name shows whether the skill has an LLM-enriched
        <code> .v2.json</code> sidecar.
      </p>

      {loading && <div className="muted">Loading...</div>}
      {error && (
        <pre style={{ color: "var(--danger)", fontSize: 12 }}>{error}</pre>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {skills.length === 0 && !loading && (
          <div className="muted">
            No skills yet. Go to <a href="/teach">Teach</a> to record one.
          </div>
        )}
        {skills.map((s) => (
          <div
            key={s.id}
            className="surface-card"
            style={{ padding: 14 }}
          >
            <div
              style={{
                display: "flex",
                gap: 10,
                alignItems: "baseline",
                flexWrap: "wrap",
              }}
            >
              <div
                style={{
                  color: "var(--text-bright)",
                  fontSize: 15,
                  fontWeight: 500,
                }}
              >
                {s.name}
              </div>
              {s.has_sidecar ? (
                <span className="tag">v2 enriched</span>
              ) : (
                <span className="tag tag-warn">v1 only</span>
              )}
              {s.destructive_action_count > 0 && (
                <span className="tag tag-danger">
                  {s.destructive_action_count} destructive
                </span>
              )}
              <span className="muted" style={{ fontSize: 12 }}>
                {s.step_count} steps · {s.param_count} params
              </span>
            </div>
            {s.description && (
              <div
                className="muted"
                style={{ fontSize: 13, marginTop: 6, lineHeight: 1.5 }}
              >
                {s.description}
              </div>
            )}
            <div
              className="muted"
              style={{ fontSize: 11, marginTop: 8 }}
            >
              <code>{s.path}</code>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
