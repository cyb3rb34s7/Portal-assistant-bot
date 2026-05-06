import { useEffect, useRef, useState } from "react";
import { useReplayStore } from "../state/replayStore";
import type {
  AmbiguousCandidate,
  ClarifyAskEvent,
  PausedEvent,
  PlanProposedEvent,
  ReportReadyEvent,
  StepFailedEvent,
} from "../protocol/types";

// ----- Page ------------------------------------------------------------

export default function ReplayPage() {
  const stage = useReplayStore((s) => s.stage);
  const reset = useReplayStore((s) => s.reset);

  // Reset everything when the page unmounts so navigating away mid-task
  // doesn't leave a dangling subscription. Any in-flight task on the
  // backend is left alone -- another mount will pick it up via
  // /api/tasks/active. (That hookup is a follow-up; v1 expects the
  // operator to stay on this page during a run.)
  useEffect(() => {
    return () => reset();
  }, [reset]);

  return (
    <div style={{ maxWidth: 980 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 18,
        }}
      >
        <div>
          <h1
            style={{
              color: "var(--text-bright)",
              fontSize: 24,
              margin: 0,
              fontWeight: 500,
            }}
          >
            Replay
          </h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Type a goal, the planner picks the right skill, watch it drive
            the portal in real time.
          </p>
        </div>
        <StageBadge stage={stage} />
      </div>

      {stage === "idle" || stage === "failed" || stage === "completed" || stage === "cancelled" ? (
        <GoalForm />
      ) : (
        <RunView />
      )}

      {stage === "completed" && <ReportPanel />}
      {stage === "failed" && <FailurePanel />}

      <ClarifyModal />
      <PlanApprovalModal />
      <PausedModal />
    </div>
  );
}

// ----- Stage badge -----------------------------------------------------

function StageBadge({ stage }: { stage: string }) {
  const labels: Record<string, { text: string; cls: string }> = {
    idle: { text: "Idle", cls: "tag" },
    submitting: { text: "Submitting", cls: "tag" },
    intake: { text: "Intake", cls: "tag" },
    clarifying: { text: "Clarifying", cls: "tag tag-warn" },
    awaiting_plan_approval: { text: "Plan ready", cls: "tag tag-warn" },
    running: { text: "Running", cls: "tag" },
    paused: { text: "Paused", cls: "tag tag-warn" },
    completed: { text: "Completed", cls: "tag tag-ok" },
    failed: { text: "Failed", cls: "tag tag-danger" },
    cancelled: { text: "Cancelled", cls: "tag tag-danger" },
  };
  const info = labels[stage] || { text: stage, cls: "tag" };
  return <span className={info.cls}>{info.text}</span>;
}

// ----- Goal form -------------------------------------------------------

function GoalForm() {
  const submit = useReplayStore((s) => s.submit);
  const reset = useReplayStore((s) => s.reset);
  const stage = useReplayStore((s) => s.stage);

  const [goal, setGoal] = useState("");
  const [portalId, setPortalId] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [portals, setPortals] = useState<
    { id: string; name: string; base_url: string; page_count: number }[]
  >([]);

  useEffect(() => {
    fetch("/api/portals")
      .then((r) => r.json())
      .then((rows) => setPortals(rows))
      .catch(() => setPortals([]));
  }, []);

  const dropRef = useRef<HTMLDivElement | null>(null);
  function onFilesPicked(list: FileList | null) {
    if (!list) return;
    setFiles(Array.from(list));
  }

  async function onSubmit() {
    if (!goal.trim()) return;
    await submit({
      goal,
      portal_id: portalId || undefined,
      attachments: files,
      auto_approve_plan: autoApprove,
    });
  }

  return (
    <div className="surface" style={{ padding: 24 }}>
      {(stage === "completed" || stage === "failed" || stage === "cancelled") && (
        <div
          className="surface-card"
          style={{
            padding: 12,
            marginBottom: 16,
            borderColor:
              stage === "completed" ? "rgba(113,213,155,0.4)" : "rgba(239,108,108,0.4)",
          }}
        >
          <div style={{ fontSize: 13 }}>
            {stage === "completed"
              ? "Last run completed."
              : stage === "failed"
              ? "Last run failed -- see the panel below."
              : "Last run was cancelled."}
            {" "}
            <button
              className="btn"
              style={{ marginLeft: 8, padding: "2px 8px" }}
              onClick={reset}
            >
              Clear
            </button>
          </div>
        </div>
      )}

      <label
        style={{
          display: "block",
          fontSize: 13,
          color: "var(--text-bright)",
          marginBottom: 6,
        }}
      >
        Goal
      </label>
      <textarea
        rows={3}
        value={goal}
        onChange={(e) => setGoal(e.target.value)}
        placeholder='e.g. "Curate the contents in batch.csv into a featured-row layout, comment Spring drop"'
        style={{ width: "100%", marginBottom: 14 }}
        data-testid="replay-goal"
      />

      <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
        <div style={{ flex: 1 }}>
          <label
            style={{
              display: "block",
              fontSize: 13,
              color: "var(--text-bright)",
              marginBottom: 6,
            }}
          >
            Portal
          </label>
          <select
            value={portalId}
            onChange={(e) => setPortalId(e.target.value)}
            style={{ width: "100%" }}
            data-testid="replay-portal"
          >
            <option value="">— pick a portal —</option>
            {portals.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.base_url})
              </option>
            ))}
          </select>
        </div>
        <div style={{ alignSelf: "flex-end", paddingBottom: 4 }}>
          <label style={{ fontSize: 13, color: "var(--text-bright)" }}>
            <input
              type="checkbox"
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
              style={{ marginRight: 6 }}
            />
            Auto-approve plan (skip confirmation if no irreversible actions)
          </label>
        </div>
      </div>

      <div
        ref={dropRef}
        onDragOver={(e) => {
          e.preventDefault();
          dropRef.current?.classList.add("dropzone-active");
        }}
        onDragLeave={() => {
          dropRef.current?.classList.remove("dropzone-active");
        }}
        onDrop={(e) => {
          e.preventDefault();
          dropRef.current?.classList.remove("dropzone-active");
          onFilesPicked(e.dataTransfer.files);
        }}
        style={{
          border: "1px dashed var(--border)",
          borderRadius: 8,
          padding: 16,
          marginBottom: 16,
          textAlign: "center",
          fontSize: 13,
          color: "var(--text-dim)",
          background: "var(--bg-card)",
        }}
        data-testid="replay-dropzone"
      >
        {files.length === 0 ? (
          <>
            Drag attachments here or{" "}
            <label
              style={{
                color: "var(--accent)",
                cursor: "pointer",
                textDecoration: "underline",
              }}
            >
              browse
              <input
                type="file"
                multiple
                style={{ display: "none" }}
                onChange={(e) => onFilesPicked(e.target.files)}
              />
            </label>{" "}
            (CSV, PPTX, PDF, images)
          </>
        ) : (
          <div>
            <div style={{ marginBottom: 6 }}>
              {files.length} file(s) attached:
            </div>
            <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 12 }}>
              {files.map((f) => (
                <li key={f.name}>
                  <code>{f.name}</code> ({Math.round(f.size / 1024)} KB)
                </li>
              ))}
            </ul>
            <button
              className="btn"
              style={{ marginTop: 8, padding: "4px 10px" }}
              onClick={() => setFiles([])}
            >
              Clear
            </button>
          </div>
        )}
      </div>

      <button
        className="btn-primary btn"
        onClick={onSubmit}
        disabled={!goal.trim() || stage === "submitting"}
        data-testid="replay-submit"
      >
        {stage === "submitting" ? "Submitting..." : "Run replay"}
      </button>
    </div>
  );
}

// ----- Run view (live trace + log) -------------------------------------

function RunView() {
  const goal = useReplayStore((s) => s.goal);
  const portal_id = useReplayStore((s) => s.portal_id);
  const cancel = useReplayStore((s) => s.cancel);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="surface-card" style={{ padding: 14 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div>
            <div style={{ fontSize: 12, color: "var(--text-dim)" }}>Goal</div>
            <div style={{ fontSize: 14, color: "var(--text-bright)" }}>{goal}</div>
            {portal_id && (
              <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>
                Portal: <code>{portal_id}</code>
              </div>
            )}
          </div>
          <button className="btn btn-danger" onClick={cancel}>
            Cancel
          </button>
        </div>
      </div>

      <TracePane />
      <LogPane />
    </div>
  );
}

// ----- Trace pane ------------------------------------------------------

function TracePane() {
  const steps = useReplayStore((s) => s.steps);
  const stage = useReplayStore((s) => s.stage);

  const list = Array.from(steps.values()).sort((a, b) => a.idx - b.idx);

  if (list.length === 0) {
    return (
      <div className="surface-card" style={{ padding: 16, fontSize: 13 }}>
        <span className="muted">
          {stage === "intake"
            ? "Intake parsing..."
            : stage === "clarifying"
            ? "Waiting for your answer..."
            : "Waiting for plan..."}
        </span>
      </div>
    );
  }

  return (
    <div className="surface-card" style={{ padding: 0, overflow: "hidden" }}>
      <div
        style={{
          padding: "10px 14px",
          borderBottom: "1px solid var(--border)",
          color: "var(--text-bright)",
          fontSize: 13,
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>Plan steps</span>
        <span className="muted" style={{ fontSize: 12 }}>
          {list.filter((s) => s.state === "succeeded").length}/{list.length} done
        </span>
      </div>
      <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
        {list.map((step) => (
          <StepRow key={step.idx} step={step} />
        ))}
      </ul>
    </div>
  );
}

function StepRow({
  step,
}: {
  step: ReturnType<typeof useReplayStore.getState>["steps"] extends Map<number, infer T>
    ? T
    : never;
}) {
  const stateColor: Record<string, string> = {
    pending: "var(--text-dim)",
    running: "var(--accent)",
    succeeded: "var(--ok)",
    failed: "var(--danger)",
    skipped: "var(--text-dim)",
  };
  return (
    <li
      style={{
        padding: "10px 14px",
        borderTop: "1px solid var(--border)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: stateColor[step.state] || "var(--text-dim)",
            flexShrink: 0,
          }}
        />
        <code style={{ color: "var(--text-bright)", fontSize: 13 }}>
          {String(step.idx).padStart(2, "0")}
        </code>
        <span style={{ color: "var(--text-bright)", fontSize: 13 }}>
          {step.skill_id}
        </span>
        <span className="muted" style={{ fontSize: 12 }}>
          {Object.entries(step.params)
            .map(([k, v]) => `${k}=${shortVal(v)}`)
            .join(", ")}
        </span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: stateColor[step.state] }}>
          {step.state}
          {typeof step.duration_ms === "number" && step.duration_ms > 0
            ? ` (${step.duration_ms}ms)`
            : ""}
        </span>
      </div>

      {step.last_action && step.state === "running" && (
        <div
          style={{
            marginTop: 6,
            marginLeft: 18,
            fontSize: 12,
            color: "var(--text-dim)",
          }}
        >
          → {step.last_action}
          {step.last_test_id ? <code> {step.last_test_id}</code> : null}
        </div>
      )}

      {step.heals.length > 0 && (
        <div style={{ marginTop: 6, marginLeft: 18 }}>
          {step.heals.map((h, i) => (
            <div
              key={i}
              style={{
                fontSize: 12,
                color: "var(--warn)",
                background: "rgba(244,185,109,0.06)",
                border: "1px solid rgba(244,185,109,0.2)",
                borderRadius: 6,
                padding: "4px 8px",
                margin: "4px 0",
              }}
            >
              <strong>healed</strong> ({h.confidence}, post-condition{" "}
              {h.post_condition_passed ? "passed" : "failed"}):{" "}
              <code>{h.original_summary}</code> → <code>{h.new_summary}</code>
              {h.persisted_to_skill ? " · saved to skill" : ""}
            </div>
          ))}
        </div>
      )}

      {step.last_failure && step.state === "failed" && (
        <div
          style={{
            marginTop: 6,
            marginLeft: 18,
            fontSize: 12,
            color: "var(--danger)",
            background: "rgba(239,108,108,0.06)",
            border: "1px solid rgba(239,108,108,0.2)",
            borderRadius: 6,
            padding: "6px 8px",
          }}
        >
          <strong>{step.last_failure.error_kind}:</strong>{" "}
          {step.last_failure.error_message}
        </div>
      )}
    </li>
  );
}

function shortVal(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return s.length > 30 ? s.slice(0, 27) + "..." : s;
}

// ----- Log pane --------------------------------------------------------

function LogPane() {
  const log = useReplayStore((s) => s.log);
  if (log.length === 0) return null;
  return (
    <div className="surface-card" style={{ padding: 14, fontSize: 12 }}>
      <div
        style={{
          color: "var(--text-bright)",
          marginBottom: 6,
          fontSize: 13,
        }}
      >
        Agent log
      </div>
      <div
        style={{
          maxHeight: 240,
          overflow: "auto",
          fontFamily: "ui-monospace, Consolas, monospace",
        }}
      >
        {log.slice(-50).map((entry, i) => (
          <div key={i} style={{ color: levelColor(entry.level) }}>
            <span style={{ color: "var(--text-dim)" }}>
              {entry.ts.replace("T", " ").slice(11, 19)}
            </span>{" "}
            <span>[{entry.level}]</span>{" "}
            {entry.message}
            {entry.source ? <span className="muted"> · {entry.source}</span> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function levelColor(level: string): string {
  if (level === "error") return "var(--danger)";
  if (level === "warn") return "var(--warn)";
  if (level === "debug") return "var(--text-dim)";
  return "var(--text)";
}

// ----- Modals: Clarify -------------------------------------------------

function ClarifyModal() {
  const pending = useReplayStore((s) => s.pending_clarify);
  const answer = useReplayStore((s) => s.answerClarify);
  const [custom, setCustom] = useState("");

  if (!pending) return null;
  return <ClarifyModalInner pending={pending} answer={answer} custom={custom} setCustom={setCustom} />;
}

function ClarifyModalInner({
  pending,
  answer,
  custom,
  setCustom,
}: {
  pending: ClarifyAskEvent;
  answer: ReturnType<typeof useReplayStore.getState>["answerClarify"];
  custom: string;
  setCustom: (s: string) => void;
}) {
  const cancel = useReplayStore((s) => s.cancel);
  return (
    <ModalShell title="Clarify" priority={pending.priority}>
      <p style={{ fontSize: 14, color: "var(--text-bright)", margin: "0 0 12px" }}>
        {pending.question}
      </p>

      {pending.options && pending.options.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: "0 0 14px" }}>
          {pending.options.map((opt) => (
            <li key={opt.value} style={{ marginBottom: 6 }}>
              <button
                className="btn"
                style={{ width: "100%", justifyContent: "flex-start", textAlign: "left" }}
                onClick={() =>
                  answer({
                    question_id: pending.id,
                    answer_value: opt.value,
                    answer_label: opt.label,
                  })
                }
                data-testid={`clarify-option-${opt.value}`}
              >
                <span style={{ fontWeight: 500 }}>{opt.label}</span>
                {opt.detail && (
                  <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                    {opt.detail}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}

      {pending.allow_custom_answer && (
        <div style={{ display: "flex", gap: 8 }}>
          <input
            type="text"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            placeholder="Type your own answer..."
            style={{ flex: 1 }}
            data-testid="clarify-custom"
          />
          <button
            className="btn-primary btn"
            onClick={() => {
              if (!custom.trim()) return;
              answer({
                question_id: pending.id,
                answer_value: custom.trim(),
              });
              setCustom("");
            }}
            disabled={!custom.trim()}
          >
            Submit
          </button>
        </div>
      )}

      {/* Belt-and-suspenders cancel: the page-level Cancel button is
          obscured by the modal backdrop, so a clarify loop without a
          good answer would otherwise trap the operator. */}
      <div style={{ marginTop: 14, textAlign: "right" }}>
        <button
          className="btn btn-danger"
          onClick={cancel}
          style={{ padding: "4px 10px", fontSize: 12 }}
          data-testid="clarify-cancel-task"
        >
          Cancel task
        </button>
      </div>
    </ModalShell>
  );
}

// ----- Modals: Plan approval -------------------------------------------

function PlanApprovalModal() {
  const stage = useReplayStore((s) => s.stage);
  const plan = useReplayStore((s) => s.plan);
  const approve = useReplayStore((s) => s.approvePlan);
  const reject = useReplayStore((s) => s.rejectPlan);
  const [rejectReason, setRejectReason] = useState("");

  if (stage !== "awaiting_plan_approval" || !plan) return null;

  return <PlanApprovalModalInner plan={plan} approve={approve} reject={reject} rejectReason={rejectReason} setRejectReason={setRejectReason} />;
}

function PlanApprovalModalInner({
  plan,
  approve,
  reject,
  rejectReason,
  setRejectReason,
}: {
  plan: PlanProposedEvent;
  approve: () => Promise<void>;
  reject: (reason: string) => Promise<void>;
  rejectReason: string;
  setRejectReason: (s: string) => void;
}) {
  const irreversible = plan.destructive_actions.filter((d) => !d.reversible);
  const reversible = plan.destructive_actions.filter((d) => d.reversible);

  return (
    <ModalShell title="Approve plan">
      <div style={{ marginBottom: 12 }}>
        <div style={{ color: "var(--text-bright)", fontSize: 14 }}>
          {plan.summary}
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {plan.steps.length} step{plan.steps.length === 1 ? "" : "s"} ·{" "}
          ~{plan.estimated_duration_seconds}s estimated
        </div>
      </div>

      {irreversible.length > 0 && (
        <div
          style={{
            background: "rgba(239,108,108,0.08)",
            border: "1px solid rgba(239,108,108,0.3)",
            borderRadius: 8,
            padding: 12,
            marginBottom: 12,
          }}
        >
          <div
            style={{ color: "var(--danger)", fontSize: 13, marginBottom: 4, fontWeight: 500 }}
          >
            ⚠ Irreversible action(s)
          </div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {irreversible.map((d, i) => (
              <li key={i}>
                Step {d.step_idx}: {d.label || d.kind}
              </li>
            ))}
          </ul>
        </div>
      )}

      {reversible.length > 0 && (
        <div className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
          Reversible actions:{" "}
          {reversible.map((d) => d.label || d.kind).join(", ")}
        </div>
      )}

      <details style={{ marginBottom: 14, fontSize: 13 }}>
        <summary style={{ cursor: "pointer" }}>Plan steps</summary>
        <ol style={{ paddingLeft: 18, marginTop: 8 }}>
          {plan.steps.map((s) => (
            <li key={s.idx}>
              <code>{s.skill_id}</code>
              <span className="muted">
                {" "}
                {Object.entries(s.params)
                  .map(([k, v]) => `${k}=${shortVal(v)}`)
                  .join(", ")}
              </span>
              {s.notes && (
                <div className="muted" style={{ fontSize: 12 }}>
                  {s.notes}
                </div>
              )}
            </li>
          ))}
        </ol>
      </details>

      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button
          className="btn-primary btn"
          onClick={approve}
          data-testid="plan-approve"
        >
          Approve & run
        </button>
        <input
          type="text"
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          placeholder="Reject reason (optional)"
          style={{ flex: 1 }}
        />
        <button
          className="btn btn-danger"
          onClick={() => reject(rejectReason || "operator rejected")}
          data-testid="plan-reject"
        >
          Reject
        </button>
      </div>
    </ModalShell>
  );
}

// ----- Modals: Paused (step failed / interrupt) -----------------------

function PausedModal() {
  const stage = useReplayStore((s) => s.stage);
  const pending = useReplayStore((s) => s.pending_pause);
  const lastFailure = useReplayStore((s) => s.last_failure);
  const resolve = useReplayStore((s) => s.resolvePause);

  if (stage !== "paused" || !pending) return null;
  return <PausedModalInner pending={pending} lastFailure={lastFailure} resolve={resolve} />;
}

function PausedModalInner({
  pending,
  lastFailure,
  resolve,
}: {
  pending: PausedEvent;
  lastFailure: StepFailedEvent | null;
  resolve: ReturnType<typeof useReplayStore.getState>["resolvePause"];
}) {
  // Pull error_details either from the linked step.failed event OR from
  // the pause's own context (orchestrator copies them in).
  const errorDetails =
    (lastFailure?.error_details as Record<string, unknown> | undefined) ||
    (pending.context.error_details as Record<string, unknown> | undefined);
  const errorKind =
    lastFailure?.error_kind ||
    (pending.context.error_kind as string | undefined);

  // ambiguous_target -> render row picker
  if (errorKind === "ambiguous_target" && errorDetails) {
    const candidates = (errorDetails.candidates || []) as AmbiguousCandidate[];
    return (
      <ModalShell title="Pick which to use">
        <p
          style={{ fontSize: 13, color: "var(--text-bright)", margin: "0 0 6px" }}
        >
          The recording targeted one specific element, but replay finds
          {" "}
          <strong>{candidates.length}</strong> matches. Pick the one you want.
        </p>
        {lastFailure?.error_message && (
          <p className="muted" style={{ fontSize: 12, margin: "0 0 12px" }}>
            {lastFailure.error_message}
          </p>
        )}

        <ul style={{ listStyle: "none", padding: 0, margin: "0 0 14px" }}>
          {candidates.map((c) => (
            <li key={c.index} style={{ marginBottom: 6 }}>
              <button
                className="btn"
                style={{
                  width: "100%",
                  justifyContent: "flex-start",
                  textAlign: "left",
                  whiteSpace: "normal",
                }}
                onClick={() =>
                  resolve({
                    action: "use_alternate",
                    payload: { candidate_index: c.index, candidate: c },
                  })
                }
                data-testid={`pause-candidate-${c.index}`}
              >
                <div>
                  <code style={{ color: "var(--accent)" }}>
                    {c.test_id || c.id || `#${c.index}`}
                  </code>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {c.text || `<${c.tag}>`}
                  </div>
                </div>
              </button>
            </li>
          ))}
        </ul>

        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn"
            onClick={() => resolve({ action: "skip" })}
            data-testid="pause-skip"
          >
            Skip step
          </button>
          <button
            className="btn btn-danger"
            onClick={() => resolve({ action: "abort" })}
            data-testid="pause-abort"
          >
            Abort run
          </button>
        </div>
      </ModalShell>
    );
  }

  // Generic step failure -> retry / skip / abort
  return (
    <ModalShell title="Step paused">
      <p style={{ fontSize: 13, color: "var(--text-bright)", margin: "0 0 6px" }}>
        The runner paused for operator decision.
      </p>
      {lastFailure ? (
        <div
          style={{
            background: "rgba(239,108,108,0.08)",
            border: "1px solid rgba(239,108,108,0.3)",
            borderRadius: 8,
            padding: 10,
            margin: "8px 0 14px",
            fontSize: 13,
          }}
        >
          <div style={{ color: "var(--danger)", fontWeight: 500 }}>
            {lastFailure.error_kind}
          </div>
          <div style={{ marginTop: 4 }}>{lastFailure.error_message}</div>
          {lastFailure.screenshot_path && (
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              Screenshot: <code>{lastFailure.screenshot_path}</code>
            </div>
          )}
        </div>
      ) : (
        <p className="muted" style={{ fontSize: 12, margin: "0 0 14px" }}>
          Reason: {pending.reason}
        </p>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <button
          className="btn-primary btn"
          onClick={() => resolve({ action: "retry" })}
          data-testid="pause-retry"
        >
          Retry
        </button>
        <button
          className="btn"
          onClick={() => resolve({ action: "skip" })}
          data-testid="pause-skip"
        >
          Skip
        </button>
        <button
          className="btn btn-danger"
          onClick={() => resolve({ action: "abort" })}
          data-testid="pause-abort"
        >
          Abort
        </button>
      </div>
    </ModalShell>
  );
}

// ----- Report panel + failure panel ------------------------------------

function ReportPanel() {
  const report = useReplayStore((s) => s.report);
  const reset = useReplayStore((s) => s.reset);
  if (!report) return null;
  return <ReportPanelInner report={report} reset={reset} />;
}

function ReportPanelInner({
  report,
  reset,
}: {
  report: ReportReadyEvent;
  reset: () => void;
}) {
  const [body, setBody] = useState<string>("Loading...");
  useEffect(() => {
    fetch(`/api/sessions/${report.session_id}/report`)
      .then((r) => r.text())
      .then(setBody)
      .catch((e) => setBody("Failed to load report: " + String(e)));
  }, [report.session_id]);

  return (
    <div className="surface" style={{ padding: 18, marginTop: 14 }}>
      <div
        style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}
      >
        <div>
          <div style={{ color: "var(--text-bright)", fontSize: 14, fontWeight: 500 }}>
            Report — {report.summary}
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {report.report_path}
          </div>
        </div>
        <button className="btn" onClick={reset}>
          New replay
        </button>
      </div>
      <pre
        style={{
          maxHeight: 400,
          overflow: "auto",
          fontSize: 12,
        }}
      >
        {body}
      </pre>
    </div>
  );
}

function FailurePanel() {
  const msg = useReplayStore((s) => s.task_failure_message);
  const reset = useReplayStore((s) => s.reset);
  if (!msg) return null;
  return (
    <div
      className="surface"
      style={{
        padding: 18,
        marginTop: 14,
        borderColor: "rgba(239,108,108,0.4)",
      }}
    >
      <div style={{ color: "var(--danger)", fontSize: 14, fontWeight: 500 }}>
        Task failed
      </div>
      <div style={{ fontSize: 13, marginTop: 6 }}>{msg}</div>
      <button className="btn" onClick={reset} style={{ marginTop: 10 }}>
        Try again
      </button>
    </div>
  );
}

// ----- ModalShell ------------------------------------------------------

function ModalShell({
  title,
  priority,
  children,
}: {
  title: string;
  priority?: "high" | "medium" | "low";
  children: React.ReactNode;
}) {
  const accent =
    priority === "high"
      ? "rgba(239,108,108,0.5)"
      : priority === "medium"
      ? "rgba(244,185,109,0.5)"
      : "rgba(106,163,255,0.4)";
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
      data-testid="modal-shell"
    >
      <div
        className="surface"
        style={{
          width: "min(560px, 92vw)",
          maxHeight: "85vh",
          overflow: "auto",
          padding: 22,
          borderColor: accent,
        }}
      >
        <div
          style={{
            color: "var(--text-bright)",
            fontSize: 16,
            fontWeight: 500,
            marginBottom: 14,
          }}
        >
          {title}
        </div>
        {children}
      </div>
    </div>
  );
}
