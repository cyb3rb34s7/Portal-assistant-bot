import { Link } from "react-router-dom";

export default function HomePage() {
  return (
    <div style={{ maxWidth: 760 }}>
      <h1
        style={{
          color: "var(--text-bright)",
          fontSize: 28,
          margin: "0 0 8px",
          fontWeight: 500,
        }}
      >
        CurationPilot
      </h1>
      <p className="muted" style={{ marginBottom: 24 }}>
        Local supervised browser automation. Record a portal workflow once,
        replay it with natural-language goals against new inputs.
      </p>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 14,
        }}
      >
        <Card
          to="/teach"
          title="Teach a new skill"
          body="Launch the portal browser, perform a workflow, save it as a reusable parameterised skill."
          accent
        />
        <Card
          to="/replay"
          title="Replay with natural language"
          body="Type a goal, attach inputs, the agent picks the right skill and drives the portal."
        />
        <Card
          to="/skills"
          title="Skill library"
          body="Browse what's been recorded. Inspect parameters, alias maps, destructive actions."
        />
        <Card
          to="/sessions"
          title="Session history"
          body="Past runs with reports, screenshots, and audit logs."
        />
      </div>
    </div>
  );
}

function Card({
  to,
  title,
  body,
  accent,
}: {
  to: string;
  title: string;
  body: string;
  accent?: boolean;
}) {
  return (
    <Link
      to={to}
      className="surface-card"
      style={{
        padding: 18,
        textDecoration: "none",
        color: "inherit",
        borderColor: accent ? "rgba(106,163,255,0.4)" : undefined,
        background: accent ? "rgba(106,163,255,0.06)" : undefined,
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <div
        style={{
          color: "var(--text-bright)",
          fontSize: 16,
          fontWeight: 500,
        }}
      >
        {title}
      </div>
      <div className="muted" style={{ fontSize: 14, lineHeight: 1.45 }}>
        {body}
      </div>
    </Link>
  );
}
