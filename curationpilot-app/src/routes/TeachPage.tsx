import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { usePortalStore } from "../state/portalStore";
import { useTeachStore } from "../state/teachStore";

type Step = 1 | 2 | 3 | 4 | 5;

export default function TeachPage() {
  const [step, setStep] = useState<Step>(1);
  const [skillName, setSkillName] = useState("");
  // Default both empty — the operator must pick a portal (or override
  // with a custom URL). No more "implicit localhost" baked into the UI.
  const [portalId, setPortalId] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [targetTab, setTargetTab] = useState<string | null>(null);

  return (
    <div style={{ maxWidth: 820 }}>
      <h1
        style={{
          color: "var(--text-bright)",
          fontSize: 24,
          margin: "0 0 6px",
          fontWeight: 500,
        }}
      >
        Teach a skill
      </h1>
      <p className="muted" style={{ marginBottom: 24 }}>
        Five-step wizard. Launch the portal, sign in, record what you do,
        annotate, save.
      </p>

      <Stepper current={step} />

      <div className="surface" style={{ padding: 24, marginTop: 18 }}>
        {step === 1 && (
          <Step1
            skillName={skillName}
            setSkillName={setSkillName}
            portalId={portalId}
            setPortalId={setPortalId}
            baseUrl={baseUrl}
            setBaseUrl={setBaseUrl}
            onNext={() => setStep(2)}
          />
        )}
        {step === 2 && (
          <Step2
            baseUrl={baseUrl}
            onTabPicked={(t) => setTargetTab(t)}
            onNext={() => setStep(3)}
            onBack={() => setStep(1)}
          />
        )}
        {step === 3 && (
          <Step3
            skillName={skillName}
            portalId={portalId}
            baseUrl={targetTab || baseUrl}
            onNext={() => setStep(4)}
            onBack={() => setStep(2)}
          />
        )}
        {step === 4 && (
          <Step4
            onNext={() => setStep(5)}
            onBack={() => setStep(3)}
          />
        )}
        {step === 5 && <Step5 onRestart={() => setStep(1)} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stepper UI
// ---------------------------------------------------------------------------

function Stepper({ current }: { current: Step }) {
  const labels = [
    "1. Launch browser",
    "2. Verify connection",
    "3. Record",
    "4. Annotate",
    "5. Save",
  ];
  return (
    <div style={{ display: "flex", gap: 6 }}>
      {labels.map((label, i) => {
        const n = (i + 1) as Step;
        const active = n === current;
        const done = n < current;
        return (
          <div
            key={label}
            style={{
              flex: 1,
              padding: "10px 12px",
              borderRadius: 6,
              fontSize: 13,
              border: "1px solid var(--border)",
              background: active
                ? "rgba(106,163,255,0.1)"
                : done
                ? "rgba(113,213,155,0.06)"
                : "var(--bg-elev)",
              color: active
                ? "var(--accent)"
                : done
                ? "var(--ok)"
                : "var(--text-dim)",
              borderColor: active
                ? "rgba(106,163,255,0.5)"
                : "var(--border)",
            }}
          >
            {label}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — name + launch browser
// ---------------------------------------------------------------------------

function Step1({
  skillName,
  setSkillName,
  portalId,
  setPortalId,
  baseUrl,
  setBaseUrl,
  onNext,
}: {
  skillName: string;
  setSkillName: (s: string) => void;
  portalId: string;
  setPortalId: (s: string) => void;
  baseUrl: string;
  setBaseUrl: (s: string) => void;
  onNext: () => void;
}) {
  const portal = usePortalStore();
  const ready = portal.state?.status === "running";

  // Load portal contexts from /api/portals so the operator picks
  // an existing portal instead of typing a URL by hand. The "Custom"
  // option lets them override with an arbitrary URL when the portal
  // hasn't been catalogued yet (e.g. first-time exploration).
  const [portals, setPortals] = useState<
    { id: string; name: string; base_url: string; page_count: number }[]
  >([]);
  const [customMode, setCustomMode] = useState(false);

  useEffect(() => {
    fetch("/api/portals")
      .then((r) => r.json())
      .then((rows) => setPortals(rows))
      .catch(() => setPortals([]));
  }, []);

  function handlePortalSelection(value: string) {
    if (value === "__custom__") {
      setCustomMode(true);
      setPortalId("");
      setBaseUrl("");
      return;
    }
    const p = portals.find((x) => x.id === value);
    if (!p) return;
    setCustomMode(false);
    setPortalId(p.id);
    setBaseUrl(p.base_url);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <Field label="Skill name" hint="snake_case is conventional">
        <input
          type="text"
          value={skillName}
          onChange={(e) => setSkillName(e.target.value)}
          placeholder="e.g. curate_one_item"
          style={{ width: "100%" }}
        />
      </Field>

      <Field
        label="Portal"
        hint={
          portals.length === 0
            ? "no portals defined yet — see portals/<id>/context.yaml. Pick 'Custom' to enter a URL inline."
            : "URL + catalog destination come from portals/<id>/context.yaml"
        }
      >
        <select
          value={customMode ? "__custom__" : portalId}
          onChange={(e) => handlePortalSelection(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">— select a portal —</option>
          {portals.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.base_url})
            </option>
          ))}
          <option value="__custom__">Custom URL (no catalog)</option>
        </select>
      </Field>

      {customMode && (
        <>
          <Field
            label="Portal id (for the catalog)"
            hint="creates portals/<id>/catalog.yaml as snapshots accumulate"
          >
            <input
              type="text"
              value={portalId}
              onChange={(e) => setPortalId(e.target.value)}
              placeholder="e.g. tvplus, internal_cms"
              style={{ width: "100%" }}
            />
          </Field>

          <Field
            label="Portal URL"
            hint="the dedicated Chrome opens here; sign in once, the runner inherits the session"
          >
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://your-portal.example/upload"
              style={{ width: "100%" }}
            />
          </Field>
        </>
      )}

      <div className="muted" style={{ fontSize: 13, lineHeight: 1.6 }}>
        Click Launch — a separate Chrome window opens with CDP debugging.
        Sign in there (Keycloak / SSO / 2FA). When you're at the page where
        you want to start recording, come back and click Continue.
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <button
          className="btn btn-primary"
          disabled={portal.loading}
          onClick={() => portal.launch(baseUrl)}
        >
          {portal.loading
            ? "Launching..."
            : ready
            ? "Re-open browser"
            : "Launch portal browser"}
        </button>
        {ready && (
          <span className="tag tag-ok">
            running (PID {portal.state?.pid})
          </span>
        )}
        {portal.error && (
          <span className="tag tag-danger" title={portal.error}>
            error
          </span>
        )}
      </div>

      {portal.error && (
        <pre style={{ color: "var(--danger)", fontSize: 12 }}>
          {portal.error}
        </pre>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          paddingTop: 6,
          borderTop: "1px solid var(--border)",
        }}
      >
        <button
          className="btn btn-primary"
          onClick={onNext}
          disabled={!skillName.trim() || !baseUrl.trim() || !ready}
        >
          Continue →
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — doctor / pick the tab
// ---------------------------------------------------------------------------

function Step2({
  baseUrl,
  onTabPicked,
  onNext,
  onBack,
}: {
  baseUrl: string;
  onTabPicked: (url: string) => void;
  onNext: () => void;
  onBack: () => void;
}) {
  const portal = usePortalStore();
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    portal.refreshDoctor();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tabs = portal.doctor?.tabs ?? [];
  const matchingTabs = baseUrl
    ? tabs.filter((t) => t.url && t.url.includes(stripScheme(baseUrl)))
    : tabs;
  const candidates = matchingTabs.length > 0 ? matchingTabs : tabs;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div>
        <button
          className="btn"
          onClick={() => portal.refreshDoctor()}
          disabled={portal.loading}
        >
          {portal.loading ? "Probing..." : "Refresh"}
        </button>
        {portal.doctor && (
          <span style={{ marginLeft: 12 }} className="muted">
            CDP {portal.doctor.connected ? (
              <span className="tag tag-ok">connected</span>
            ) : (
              <span className="tag tag-danger">disconnected</span>
            )}
            {portal.doctor.browser_version && (
              <span style={{ marginLeft: 8, fontSize: 12 }}>
                {portal.doctor.browser_version}
              </span>
            )}
          </span>
        )}
      </div>

      {portal.doctor?.error && (
        <pre style={{ color: "var(--danger)", fontSize: 12 }}>
          {portal.doctor.error}
        </pre>
      )}

      <div className="muted" style={{ fontSize: 13 }}>
        Pick the tab you want to record against. We'll attach the recorder
        to that page; other tabs are untouched.
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {candidates.length === 0 && (
          <div className="muted">
            No matching tabs. Open one in the launched browser and refresh.
          </div>
        )}
        {candidates.map((t) => (
          <label
            key={t.id}
            className="surface-card"
            style={{
              display: "flex",
              gap: 10,
              padding: 12,
              cursor: "pointer",
              borderColor:
                picked === t.url
                  ? "rgba(106,163,255,0.5)"
                  : "var(--border)",
            }}
          >
            <input
              type="radio"
              name="tab"
              checked={picked === t.url}
              onChange={() => {
                setPicked(t.url);
                onTabPicked(t.url);
              }}
            />
            <div style={{ flex: 1 }}>
              <div
                style={{
                  fontSize: 14,
                  color: "var(--text-bright)",
                  fontWeight: 500,
                }}
              >
                {t.title || "(untitled)"}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {t.url}
              </div>
            </div>
          </label>
        ))}
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          paddingTop: 6,
          borderTop: "1px solid var(--border)",
        }}
      >
        <button className="btn" onClick={onBack}>
          ← Back
        </button>
        <button className="btn btn-primary" onClick={onNext} disabled={!picked}>
          Continue →
        </button>
      </div>
    </div>
  );
}

function stripScheme(url: string): string {
  return url.replace(/^https?:\/\//, "");
}

// ---------------------------------------------------------------------------
// Step 3 — recording
// ---------------------------------------------------------------------------

function Step3({
  skillName,
  portalId,
  baseUrl,
  onNext,
  onBack,
}: {
  skillName: string;
  portalId: string;
  baseUrl: string;
  onNext: () => void;
  onBack: () => void;
}) {
  const teach = useTeachStore();
  const recording = teach.stage === "recording";
  const stopped = teach.stage === "stopping" || teach.stage === "ready_to_record";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div
        className="surface-card"
        style={{
          padding: 14,
          borderColor: "rgba(244,185,109,0.4)",
          background: "rgba(244,185,109,0.05)",
        }}
      >
        <div style={{ color: "var(--warn)", fontWeight: 500, marginBottom: 4 }}>
          Heads up
        </div>
        <div style={{ fontSize: 13, lineHeight: 1.6 }}>
          Once recording starts, every meaningful click and form fill in the
          launched browser becomes part of the skill. Don't navigate
          anywhere unrelated, don't switch tabs, don't open new pages
          unless they're part of the workflow you're teaching.
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        {!recording && !stopped && (
          <button
            className="btn btn-primary"
            onClick={() =>
              teach.start({
                skill_name: skillName,
                portal_id: portalId || undefined,
                base_url: baseUrl,
              })
            }
          >
            Start recording
          </button>
        )}
        {recording && (
          <>
            <button className="btn btn-warn" onClick={() => teach.stop()}>
              Stop recording
            </button>
            <span
              className="tag"
              style={{
                background: "rgba(239,108,108,0.12)",
                color: "var(--danger)",
                borderColor: "rgba(239,108,108,0.3)",
              }}
            >
              ● recording
            </span>
            <span className="muted" style={{ fontSize: 13 }}>
              {teach.event_count} events captured
            </span>
          </>
        )}
        {stopped && (
          <>
            <span className="tag tag-ok">stopped</span>
            <span className="muted" style={{ fontSize: 13 }}>
              {teach.stop_response?.event_count ?? teach.event_count} events
              total. Session id:{" "}
              <code>{teach.session_id}</code>
            </span>
          </>
        )}
      </div>

      {teach.error && (
        <pre style={{ color: "var(--danger)", fontSize: 12 }}>
          {teach.error}
        </pre>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          paddingTop: 6,
          borderTop: "1px solid var(--border)",
        }}
      >
        <button className="btn" onClick={onBack} disabled={recording}>
          ← Back
        </button>
        <button
          className="btn btn-primary"
          onClick={onNext}
          disabled={!stopped}
        >
          Annotate →
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 4 — annotate (auto + LLM)
// ---------------------------------------------------------------------------

function Step4({
  onNext,
  onBack,
}: {
  onNext: () => void;
  onBack: () => void;
}) {
  const teach = useTeachStore();
  const [useLlm, setUseLlm] = useState(true);
  const annotating = teach.stage === "annotating";
  const done = teach.stage === "complete";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="muted" style={{ fontSize: 13, lineHeight: 1.6 }}>
        Auto-annotate filters noise + builds a parameterised skill from the
        recording. The LLM enrichment renames auto-generated parameter
        names (<code>slot_1_content_select</code>) into semantic ones
        (<code>slot_1_content_id</code>) so the planner can drive the
        skill from natural language.
      </div>

      <label
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          fontSize: 14,
        }}
      >
        <input
          type="checkbox"
          checked={useLlm}
          onChange={(e) => setUseLlm(e.target.checked)}
          disabled={annotating || done}
        />
        Run LLM enrichment via Groq (<code>annotate_skill_llm</code>)
      </label>

      <div>
        {!done && (
          <button
            className="btn btn-primary"
            onClick={() => teach.annotate(useLlm)}
            disabled={annotating}
          >
            {annotating ? "Annotating..." : "Run annotate"}
          </button>
        )}
        {done && <span className="tag tag-ok">done</span>}
      </div>

      {teach.annotate_response && (
        <div
          className="surface-card"
          style={{ padding: 14, fontSize: 13 }}
        >
          <div
            style={{
              color: "var(--text-bright)",
              fontWeight: 500,
              marginBottom: 6,
            }}
          >
            {teach.annotate_response.skill_name}
          </div>
          <div className="muted" style={{ marginBottom: 8 }}>
            {teach.annotate_response.step_count} steps,{" "}
            {teach.annotate_response.v1_param_count} v1 params
            {teach.annotate_response.v2_parameters.length > 0 && (
              <>
                {" → "}
                <span style={{ color: "var(--accent)" }}>
                  {teach.annotate_response.v2_parameters.length} v2 semantic
                  params
                </span>
              </>
            )}
            {teach.annotate_response.v2_destructive_actions.length > 0 && (
              <>
                {" · "}
                <span className="tag tag-warn">
                  {teach.annotate_response.v2_destructive_actions.length}{" "}
                  destructive
                </span>
              </>
            )}
          </div>
          {teach.annotate_response.annotate_llm_error && (
            <pre style={{ color: "var(--danger)", fontSize: 12 }}>
              LLM enrichment failed:{" "}
              {teach.annotate_response.annotate_llm_error}
            </pre>
          )}
          {teach.annotate_response.v2_parameters.length > 0 && (
            <table
              style={{
                width: "100%",
                fontSize: 12,
                borderCollapse: "collapse",
              }}
            >
              <thead>
                <tr style={{ color: "var(--text-dim)" }}>
                  <th style={th}>Param</th>
                  <th style={th}>Type</th>
                  <th style={th}>Source hint</th>
                  <th style={th}>v1 alias</th>
                </tr>
              </thead>
              <tbody>
                {teach.annotate_response.v2_parameters.map((p) => (
                  <tr key={p.name}>
                    <td style={td}>
                      <code>{p.name}</code>
                    </td>
                    <td style={td}>{p.type}</td>
                    <td style={td}>{p.source_hint || "—"}</td>
                    <td style={td}>
                      <code style={{ color: "var(--text-dim)" }}>
                        {teach.annotate_response!.v2_alias_map[p.name] || "—"}
                      </code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {teach.error && (
        <pre style={{ color: "var(--danger)", fontSize: 12 }}>
          {teach.error}
        </pre>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          paddingTop: 6,
          borderTop: "1px solid var(--border)",
        }}
      >
        <button className="btn" onClick={onBack} disabled={annotating}>
          ← Back
        </button>
        <button
          className="btn btn-primary"
          onClick={onNext}
          disabled={!done}
        >
          Continue →
        </button>
      </div>
    </div>
  );
}

const th = {
  textAlign: "left" as const,
  padding: "6px 8px",
  borderBottom: "1px solid var(--border)",
  fontWeight: 500,
};
const td = {
  padding: "6px 8px",
  borderBottom: "1px solid var(--border)",
};

// ---------------------------------------------------------------------------
// Step 5 — done
// ---------------------------------------------------------------------------

function Step5({ onRestart }: { onRestart: () => void }) {
  const teach = useTeachStore();
  const navigate = useNavigate();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div
        className="surface-card"
        style={{
          padding: 18,
          borderColor: "rgba(113,213,155,0.4)",
          background: "rgba(113,213,155,0.05)",
        }}
      >
        <div
          style={{
            color: "var(--ok)",
            fontWeight: 500,
            marginBottom: 6,
            fontSize: 16,
          }}
        >
          Skill saved
        </div>
        <div style={{ fontSize: 14, lineHeight: 1.6 }}>
          <code>{teach.skill_name}</code> is now in your skill library. Drive
          it from the Replay tab with a natural-language goal, or open the
          Skills tab to see what was captured.
        </div>
      </div>

      <div style={{ display: "flex", gap: 10 }}>
        <button
          className="btn btn-primary"
          onClick={() => {
            teach.reset();
            navigate("/skills");
          }}
        >
          Open Skills tab
        </button>
        <button
          className="btn"
          onClick={() => {
            teach.reset();
            navigate("/replay");
          }}
        >
          Try Replay
        </button>
        <button
          className="btn"
          onClick={() => {
            teach.reset();
            onRestart();
          }}
        >
          Teach another
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div
        style={{
          color: "var(--text-bright)",
          fontSize: 13,
          fontWeight: 500,
        }}
      >
        {label}
      </div>
      {hint && (
        <div className="muted" style={{ fontSize: 12 }}>
          {hint}
        </div>
      )}
      <div style={{ marginTop: 4 }}>{children}</div>
    </div>
  );
}
