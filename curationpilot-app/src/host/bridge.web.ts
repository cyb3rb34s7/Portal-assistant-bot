// Web implementation of HostBridge. Talks to the FastAPI server at
// /api/* (proxied by Vite dev server in dev; same-origin in prod).
// WebSocket /api/events streams AgentEvents to subscribers.

import type {
  AgentEvent,
  AnnotateResponse,
  DoctorResponse,
  PortalState,
  SessionSummary,
  SkillSummary,
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
