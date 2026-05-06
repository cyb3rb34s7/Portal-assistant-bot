export default function ReplayPage() {
  return (
    <div style={{ maxWidth: 720 }}>
      <h1
        style={{
          color: "var(--text-bright)",
          fontSize: 24,
          margin: "0 0 6px",
          fontWeight: 500,
        }}
      >
        Replay
      </h1>
      <p className="muted" style={{ marginBottom: 24 }}>
        Type a goal, the planner picks the right skill, watch it drive the
        portal in real time.
      </p>

      <div
        className="surface-card"
        style={{
          padding: 18,
          borderColor: "rgba(244,185,109,0.4)",
          background: "rgba(244,185,109,0.05)",
        }}
      >
        <div
          style={{
            color: "var(--warn)",
            fontWeight: 500,
            marginBottom: 6,
          }}
        >
          Coming next
        </div>
        <div style={{ fontSize: 13, lineHeight: 1.6 }}>
          Replay UI is the next feature — the FastAPI plumbing is in place
          (<code>POST /api/tasks</code> + <code>POST /api/commands</code> +
          the WebSocket event stream you can already see open in the
          dev-tools Network tab when you load this page). Until the React
          flow lands, drive replays from the terminal:
          <pre style={{ marginTop: 12 }}>
{`scripts\\replay_nl.cmd "Curate the contents in batch.csv into a featured-row layout" tests\\fixtures\\batch.csv`}
          </pre>
        </div>
      </div>
    </div>
  );
}
