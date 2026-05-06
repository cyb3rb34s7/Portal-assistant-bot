import { create } from "zustand";
import { bridge } from "../host";
import type {
  AgentEvent,
  AgentLogEvent,
  ClarifyAskEvent,
  PausedEvent,
  PlanProposedEvent,
  ReportReadyEvent,
  StepFailedEvent,
  StepHealedEvent,
  StepProgressEvent,
  StepStartedEvent,
  StepSucceededEvent,
} from "../protocol/types";

// ----- Per-step trace entry --------------------------------------------

export type StepState = "pending" | "running" | "succeeded" | "failed" | "skipped";

export interface StepTrace {
  idx: number;
  skill_id: string;
  params: Record<string, unknown>;
  state: StepState;
  duration_ms?: number;
  // Most recent micro-action (click/fill/...) the executor reported.
  // Useful for showing "what's happening right now" beneath the step row.
  last_action?: string;
  last_test_id?: string;
  last_screenshot_path?: string;
  // Heals reported by the runner. Each entry feeds an inline amber row.
  heals: StepHealedEvent[];
  // Set once a step.failed event references this idx. Holds the
  // structured error_details so the PausedModal can render a row picker.
  last_failure?: StepFailedEvent;
}

// ----- Store stage ------------------------------------------------------

export type ReplayStage =
  | "idle"
  | "submitting"
  | "intake"
  | "clarifying"
  | "awaiting_plan_approval"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

interface ReplayState {
  stage: ReplayStage;
  task_id: string | null;
  started_at: string | null;

  // ----- Goal payload (held so we can show it in the trace pane) -----
  goal: string;
  portal_id: string | null;
  attachment_names: string[];

  // ----- Plan + clarify -----
  plan: PlanProposedEvent | null;
  pending_clarify: ClarifyAskEvent | null;

  // ----- Step trace -----
  steps: Map<number, StepTrace>;

  // ----- Pause / failure -----
  pending_pause: PausedEvent | null;
  last_failure: StepFailedEvent | null;

  // ----- Final report -----
  report: ReportReadyEvent | null;
  task_failure_message: string | null;

  // ----- Live log feed (capped) -----
  log: { ts: string; level: string; message: string; source?: string }[];

  // ----- WebSocket cleanup -----
  _unsubscribe: (() => void) | null;

  // ---- Actions ----
  submit(args: {
    goal: string;
    portal_id?: string;
    attachments?: File[];
    auto_approve_plan?: boolean;
  }): Promise<void>;
  answerClarify(args: {
    question_id: string;
    answer_value: string;
    answer_label?: string;
  }): Promise<void>;
  approvePlan(): Promise<void>;
  rejectPlan(reason: string): Promise<void>;
  resolvePause(args: {
    action: "retry" | "skip" | "abort" | "use_alternate";
    payload?: Record<string, unknown>;
  }): Promise<void>;
  cancel(): Promise<void>;
  reset(): void;
}

const LOG_CAP = 200;

export const useReplayStore = create<ReplayState>((set, get) => ({
  stage: "idle",
  task_id: null,
  started_at: null,
  goal: "",
  portal_id: null,
  attachment_names: [],
  plan: null,
  pending_clarify: null,
  steps: new Map(),
  pending_pause: null,
  last_failure: null,
  report: null,
  task_failure_message: null,
  log: [],
  _unsubscribe: null,

  async submit(args) {
    const prevUnsub = get()._unsubscribe;
    if (prevUnsub) prevUnsub();

    set({
      stage: "submitting",
      task_id: null,
      goal: args.goal,
      portal_id: args.portal_id ?? null,
      attachment_names: (args.attachments || []).map((f) => f.name),
      plan: null,
      pending_clarify: null,
      steps: new Map(),
      pending_pause: null,
      last_failure: null,
      report: null,
      task_failure_message: null,
      log: [],
      _unsubscribe: null,
    });

    // Subscribe BEFORE submitting so we don't miss the first events.
    const unsub = bridge.subscribeEvents((ev) => handleEvent(get, set, ev));
    set({ _unsubscribe: unsub });

    try {
      const resp = await bridge.submitTask({
        goal: args.goal,
        portal_id: args.portal_id,
        attachments: args.attachments,
        auto_approve_plan: args.auto_approve_plan,
      });
      set({
        stage: "intake",
        task_id: resp.task_id,
        started_at: resp.started_at,
      });
    } catch (e) {
      unsub();
      set({
        stage: "failed",
        _unsubscribe: null,
        task_failure_message: (e as Error).message,
      });
    }
  },

  async answerClarify({ question_id, answer_value, answer_label }) {
    const { task_id } = get();
    if (!task_id) return;
    await bridge.submitCommand({
      type: "clarify.answer",
      task_id,
      question_id,
      answer_value,
      answer_label,
    });
    set({ pending_clarify: null });
  },

  async approvePlan() {
    const { task_id, plan } = get();
    if (!task_id || !plan) return;
    await bridge.submitCommand({
      type: "plan.approve",
      task_id,
      plan_id: plan.id,
    });
    set({ stage: "running" });
  },

  async rejectPlan(reason) {
    const { task_id, plan } = get();
    if (!task_id || !plan) return;
    await bridge.submitCommand({
      type: "plan.reject",
      task_id,
      plan_id: plan.id,
      reason,
    });
  },

  async resolvePause({ action, payload }) {
    const { task_id, pending_pause } = get();
    if (!task_id || !pending_pause) return;
    await bridge.submitCommand({
      type: "pause.resolve",
      task_id,
      pause_id: pending_pause.pause_id,
      action,
      payload: payload ?? null,
    });
    set({ pending_pause: null, stage: "running" });
  },

  async cancel() {
    const { task_id } = get();
    if (!task_id) return;
    try {
      await bridge.submitCommand({ type: "task.cancel", task_id });
    } catch {
      // ignore — server may already be tearing down
    }
  },

  reset() {
    const unsub = get()._unsubscribe;
    if (unsub) unsub();
    set({
      stage: "idle",
      task_id: null,
      started_at: null,
      goal: "",
      portal_id: null,
      attachment_names: [],
      plan: null,
      pending_clarify: null,
      steps: new Map(),
      pending_pause: null,
      last_failure: null,
      report: null,
      task_failure_message: null,
      log: [],
      _unsubscribe: null,
    });
  },
}));

// ----- Event reducer ----------------------------------------------------
//
// Kept outside the closure so the surface stays small + testable in
// isolation. All state mutation goes through `set` from the create()
// callback above.

function handleEvent(
  get: () => ReplayState,
  set: (partial: Partial<ReplayState> | ((s: ReplayState) => Partial<ReplayState>)) => void,
  ev: AgentEvent,
) {
  // Filter out events that aren't tied to our task. WebSocket fans
  // out everything published; teach.event_count or events from a
  // different task should be ignored on this page.
  const evTaskId = (ev as { task_id?: string }).task_id;
  const myTaskId = get().task_id;
  // Allow events with no task_id (agent.ready, agent.heartbeat) and
  // events that match our task. Drop others.
  if (evTaskId && myTaskId && evTaskId !== myTaskId) return;

  switch (ev.type) {
    case "agent.log": {
      pushLog(set, ev as AgentLogEvent);
      return;
    }
    case "intake.extracted": {
      pushLog(set, {
        type: "agent.log",
        level: "info",
        message: "Intake parsed entities",
      });
      return;
    }
    case "clarify.ask": {
      set({
        stage: "clarifying",
        pending_clarify: ev as ClarifyAskEvent,
      });
      return;
    }
    case "plan.proposed": {
      // Pre-seed the steps map so the trace pane has something to
      // render before the first step.started arrives.
      const pe = ev as PlanProposedEvent;
      const steps = new Map<number, StepTrace>();
      for (const s of pe.steps) {
        steps.set(s.idx, {
          idx: s.idx,
          skill_id: s.skill_id,
          params: s.params,
          state: "pending",
          heals: [],
        });
      }
      set({
        stage: "awaiting_plan_approval",
        plan: pe,
        steps,
      });
      return;
    }
    case "step.started": {
      const se = ev as StepStartedEvent;
      mutateStep(set, get, se.idx, (s) => ({ ...s, state: "running" }), {
        idx: se.idx,
        skill_id: se.skill_id,
        params: se.params,
        state: "running",
        heals: [],
      });
      set({ stage: "running" });
      return;
    }
    case "step.progress": {
      const pe = ev as StepProgressEvent;
      mutateStep(set, get, pe.idx, (s) => ({
        ...s,
        last_action: pe.action,
        last_test_id: pe.test_id,
        last_screenshot_path: pe.screenshot_path,
      }));
      return;
    }
    case "step.healed": {
      const he = ev as StepHealedEvent;
      mutateStep(set, get, he.idx, (s) => ({
        ...s,
        heals: [...s.heals, he],
      }));
      return;
    }
    case "step.succeeded": {
      const ss = ev as StepSucceededEvent;
      mutateStep(set, get, ss.idx, (s) => ({
        ...s,
        state: "succeeded",
        duration_ms: ss.duration_ms,
      }));
      return;
    }
    case "step.failed": {
      const sf = ev as StepFailedEvent;
      mutateStep(set, get, sf.idx, (s) => ({
        ...s,
        state: "failed",
        last_failure: sf,
      }));
      set({ last_failure: sf });
      return;
    }
    case "paused": {
      set({
        stage: "paused",
        pending_pause: ev as PausedEvent,
      });
      return;
    }
    case "report.ready": {
      set({ report: ev as ReportReadyEvent });
      return;
    }
    case "task.completed": {
      set({
        stage: "completed",
        pending_clarify: null,
        pending_pause: null,
      });
      return;
    }
    case "task.failed": {
      const tf = ev as { error_kind: string; error_message: string };
      set({
        stage: "failed",
        task_failure_message: `${tf.error_kind}: ${tf.error_message}`,
        pending_clarify: null,
        pending_pause: null,
      });
      return;
    }
    case "task.cancelled": {
      set({
        stage: "cancelled",
        pending_clarify: null,
        pending_pause: null,
      });
      return;
    }
    default:
      // unknown / agent.ready / heartbeat -- ignore
      return;
  }
}

function pushLog(
  set: (partial: Partial<ReplayState> | ((s: ReplayState) => Partial<ReplayState>)) => void,
  ev: { level?: string; message?: string; source?: string; ts?: string },
) {
  set((s) => ({
    log: [
      ...s.log.slice(-LOG_CAP + 1),
      {
        ts: ev.ts || new Date().toISOString(),
        level: ev.level || "info",
        message: ev.message || "",
        source: ev.source,
      },
    ],
  }));
}

function mutateStep(
  set: (partial: Partial<ReplayState> | ((s: ReplayState) => Partial<ReplayState>)) => void,
  get: () => ReplayState,
  idx: number,
  updater: (s: StepTrace) => StepTrace,
  fallback?: StepTrace,
) {
  const cur = get().steps.get(idx);
  const next = new Map(get().steps);
  if (cur) {
    next.set(idx, updater(cur));
  } else if (fallback) {
    next.set(idx, fallback);
  } else {
    return;
  }
  set({ steps: next });
}
