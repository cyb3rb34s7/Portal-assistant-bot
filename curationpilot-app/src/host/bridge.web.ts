// Web implementation of HostBridge. Talks to the FastAPI server at
// /api/* (proxied by Vite dev server in dev; same-origin in prod).
// WebSocket /api/events streams AgentEvents to subscribers.

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
import type { HostBridge } from "./bridge";

async function api<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const r = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    ...init,
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

class WebBridge implements HostBridge {
  // ---- Portal browser ----
  launchPortal(targetUrl?: string): Promise<PortalState> {
    return api<PortalState>("/portal/launch", {
      method: "POST",
      body: JSON.stringify({ target_url: targetUrl ?? null }),
    });
  }

  doctorPortal(): Promise<DoctorResponse> {
    return api<DoctorResponse>("/portal/doctor");
  }

  closePortal(): Promise<PortalState> {
    return api<PortalState>("/portal/close", { method: "POST" });
  }

  // ---- Teach ----
  teachStart(args: {
    skill_name: string;
    portal_id?: string;
    base_url?: string;
  }): Promise<TeachStartResponse> {
    return api<TeachStartResponse>("/teach/start", {
      method: "POST",
      body: JSON.stringify(args),
    });
  }

  teachStop(args: { teach_id: string }): Promise<TeachStopResponse> {
    return api<TeachStopResponse>("/teach/stop", {
      method: "POST",
      body: JSON.stringify(args),
    });
  }

  teachAnnotate(args: {
    session_id: string;
    name: string;
    llm: boolean;
    client?: string;
  }): Promise<AnnotateResponse> {
    return api<AnnotateResponse>("/teach/annotate", {
      method: "POST",
      body: JSON.stringify(args),
    });
  }

  // ---- Library + history ----
  listSkills(): Promise<SkillSummary[]> {
    return api<SkillSummary[]>("/skills");
  }

  getSkill(id: string): Promise<unknown> {
    return api<unknown>(`/skills/${encodeURIComponent(id)}`);
  }

  listSessions(): Promise<SessionSummary[]> {
    return api<SessionSummary[]>("/sessions");
  }

  async getSessionReport(id: string): Promise<string> {
    const r = await fetch(
      `/api/sessions/${encodeURIComponent(id)}/report`
    );
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }

  sessionScreenshotUrl(sessionId: string, filename: string): string {
    return `/api/sessions/${encodeURIComponent(sessionId)}/screenshots/${encodeURIComponent(filename)}`;
  }

  // ---- Replay tasks ----
  async submitTask(args: SubmitTaskArgs): Promise<SubmitTaskResponse> {
    const fd = new FormData();
    fd.append("goal", args.goal);
    if (args.portal_id) fd.append("portal_id", args.portal_id);
    fd.append(
      "auto_approve_plan",
      args.auto_approve_plan ? "true" : "false",
    );
    for (const file of args.attachments || []) {
      fd.append("attachments", file, file.name);
    }
    const r = await fetch("/api/tasks", { method: "POST", body: fd });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try {
        const body = await r.json();
        if (body?.detail) msg = body.detail;
      } catch {
        // ignore
      }
      throw new Error(msg);
    }
    return r.json() as Promise<SubmitTaskResponse>;
  }

  submitCommand(cmd: HostCommand): Promise<{ ok: boolean; queued?: string }> {
    return api<{ ok: boolean; queued?: string }>("/commands", {
      method: "POST",
      body: JSON.stringify({ v: 1, ...cmd }),
    });
  }

  getActiveTask(): Promise<ActiveTaskResponse> {
    return api<ActiveTaskResponse>("/tasks/active");
  }

  // ---- Live event stream ----
  subscribeEvents(handler: (ev: AgentEvent) => void): () => void {
    // Open a WebSocket once; all subscribers share the same socket
    // via the bus singleton in state/eventBus.ts. For now, simple
    // per-call socket — fine for a single-page app with one consumer.
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/events`);
    ws.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as AgentEvent;
        handler(ev);
      } catch {
        // ignore non-JSON frames
      }
    };
    let closed = false;
    const unsubscribe = () => {
      if (closed) return;
      closed = true;
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
    return unsubscribe;
  }
}

export const bridge: HostBridge = new WebBridge();
