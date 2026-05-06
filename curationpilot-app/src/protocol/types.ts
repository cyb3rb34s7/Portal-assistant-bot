// TypeScript mirror of the Python AgentEvent + HostCommand schemas in
// pilot/agent/schemas/protocol.py + the new /api/* shapes from
// pilot/agent/web_server.py. Keep in sync by hand for now — a future
// codegen step (scripts/gen_ts_from_pydantic.py) is on the roadmap.

export type Confidence = "high" | "medium" | "low";

export type AgentEventType =
  | "agent.ready"
  | "agent.heartbeat"
  | "agent.log"
  | "intake.extracted"
  | "clarify.ask"
  | "plan.proposed"
  | "step.started"
  | "step.progress"
  | "step.healed"
  | "step.succeeded"
  | "step.failed"
  | "paused"
  | "report.ready"
  | "task.completed"
  | "task.failed"
  | "task.cancelled"
  | "teach.event_count"; // server-side custom event for teach pump

export interface AgentEventBase {
  type: AgentEventType;
  v?: number;
  ts?: string;
  task_id?: string;
}

export interface AgentReadyEvent extends AgentEventBase {
  type: "agent.ready";
  agent_version?: string;
  capabilities?: Record<string, unknown>;
}

export interface AgentLogEvent extends AgentEventBase {
  type: "agent.log";
  level: "info" | "warn" | "error" | "debug";
  message: string;
  source?: string;
  context?: Record<string, unknown>;
}

export interface ClarifyAskEvent extends AgentEventBase {
  type: "clarify.ask";
  id: string;
  question: string;
  options: { value: string; label: string; detail?: string }[];
  allow_custom_answer: boolean;
  priority: "high" | "medium" | "low";
}

export interface PlanProposedEvent extends AgentEventBase {
  type: "plan.proposed";
  id: string;
  summary: string;
  skill_summary: { skill_id: string; invocations: number; description?: string }[];
  steps: {
    idx: number;
    skill_id: string;
    params: Record<string, unknown>;
    param_sources?: Record<string, string>;
    notes?: string;
  }[];
  destructive_actions: {
    step_idx: number;
    kind: string;
    reversible: boolean;
    label?: string;
  }[];
  estimated_duration_seconds: number;
  preconditions: string[];
}

export interface StepStartedEvent extends AgentEventBase {
  type: "step.started";
  idx: number;
  skill_id: string;
  params: Record<string, unknown>;
}

export interface StepProgressEvent extends AgentEventBase {
  type: "step.progress";
  idx: number;
  action: string;
  test_id?: string;
  screenshot_path?: string;
}

export interface StepHealedEvent extends AgentEventBase {
  type: "step.healed";
  idx: number;
  original_summary: string;
  new_summary: string;
  confidence: Confidence;
  reason: string;
  post_condition_passed: boolean;
  persisted_to_skill: boolean;
}

export interface StepSucceededEvent extends AgentEventBase {
  type: "step.succeeded";
  idx: number;
  duration_ms: number;
}

export interface StepFailedEvent extends AgentEventBase {
  type: "step.failed";
  idx: number;
  error_kind: string;
  error_message: string;
  screenshot_path?: string;
  suggestions?: { action: string; label: string; payload?: unknown }[];
}

export interface PausedEvent extends AgentEventBase {
  type: "paused";
  pause_id: string;
  reason: string;
  context: Record<string, unknown>;
}

export interface ReportReadyEvent extends AgentEventBase {
  type: "report.ready";
  session_id: string;
  report_path: string;
  summary: string;
  warnings: string[];
}

export interface TeachEventCountEvent extends AgentEventBase {
  type: "teach.event_count";
  teach_id: string;
  count: number;
}

export type AgentEvent =
  | AgentReadyEvent
  | AgentLogEvent
  | ClarifyAskEvent
  | PlanProposedEvent
  | StepStartedEvent
  | StepProgressEvent
  | StepHealedEvent
  | StepSucceededEvent
  | StepFailedEvent
  | PausedEvent
  | ReportReadyEvent
  | TeachEventCountEvent
  | AgentEventBase;

// ---- /api/portal -------------------------------------------------------

export interface PortalState {
  status: "idle" | "launching" | "running" | "closing";
  pid: number | null;
  cdp_url: string;
  target_url: string | null;
  profile_dir: string | null;
  started_at: number | null;
  last_error: string | null;
}

export interface DoctorResponse {
  connected: boolean;
  cdp_url: string;
  tabs: { id: string; url: string; title: string; type: string }[];
  error: string | null;
  browser_version: string | null;
}

// ---- /api/teach -------------------------------------------------------

export interface TeachStartResponse {
  teach_id: string;
  skill_name: string;
  session_id: string;
  started_at: string;
}

export interface TeachStopResponse {
  teach_id: string;
  session_id: string;
  event_count: number;
  skill_name: string;
}

export interface AnnotateResponse {
  skill_name: string;
  skill_path: string;
  sidecar_path: string | null;
  v1_param_count: number;
  step_count: number;
  v2_parameters: {
    name: string;
    semantic?: string;
    type: string;
    source_hint?: string;
  }[];
  v2_alias_map: Record<string, string>;
  v2_destructive_actions: { step: number; kind: string; reversible: boolean }[];
  annotate_llm_error?: string;
}

// ---- /api/skills ------------------------------------------------------

export interface SkillSummary {
  id: string;
  name: string;
  description?: string | null;
  step_count: number;
  param_count: number;
  destructive_action_count: number;
  has_sidecar: boolean;
  path: string;
  updated_at: string;
}

// ---- /api/sessions ----------------------------------------------------

export interface SessionSummary {
  id: string;
  skill_name?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  event_count?: number | null;
  has_report: boolean;
  screenshot_count: number;
}
