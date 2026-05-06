// HostBridge interface — single seam between the React app and
// whatever's hosting it (web in v1, Electron in v2). Per AD-002 in
// DOCS/CONTEXT.md, every browser-vs-Electron-sensitive capability
// goes through this interface so v2 is a drop-in port.

import type {
  ActiveTaskResponse,
  AgentEvent,
  AnnotateResponse,
  DoctorResponse,
  HostCommand,
  PortalState,
  SessionSummary,
  SkillSummary,
  SubmitTaskArgs,
  SubmitTaskResponse,
  TeachStartResponse,
  TeachStopResponse,
} from "../protocol/types";

export interface HostBridge {
  // ---- Portal browser lifecycle ----
  launchPortal(targetUrl?: string): Promise<PortalState>;
  doctorPortal(): Promise<DoctorResponse>;
  closePortal(): Promise<PortalState>;

  // ---- Teach ----
  teachStart(args: {
    skill_name: string;
    portal_id?: string;
    base_url?: string;
  }): Promise<TeachStartResponse>;
  teachStop(args: { teach_id: string }): Promise<TeachStopResponse>;
  teachAnnotate(args: {
    session_id: string;
    name: string;
    llm: boolean;
    client?: string;
  }): Promise<AnnotateResponse>;

  // ---- Library + history ----
  listSkills(): Promise<SkillSummary[]>;
  getSkill(id: string): Promise<unknown>;
  listSessions(): Promise<SessionSummary[]>;
  getSessionReport(id: string): Promise<string>;
  sessionScreenshotUrl(sessionId: string, filename: string): string;

  // ---- Replay tasks ----
  submitTask(args: SubmitTaskArgs): Promise<SubmitTaskResponse>;
  submitCommand(cmd: HostCommand): Promise<{ ok: boolean; queued?: string }>;
  getActiveTask(): Promise<ActiveTaskResponse>;

  // ---- Live event stream ----
  subscribeEvents(handler: (ev: AgentEvent) => void): () => void;
}
