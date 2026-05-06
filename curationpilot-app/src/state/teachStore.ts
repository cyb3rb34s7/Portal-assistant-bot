import { create } from "zustand";
import { bridge } from "../host";
import type {
  AgentEvent,
  AnnotateResponse,
  TeachStartResponse,
  TeachStopResponse,
} from "../protocol/types";

export type TeachStage =
  | "idle"
  | "ready_to_record"
  | "recording"
  | "stopping"
  | "annotating"
  | "complete"
  | "error";

interface TeachState {
  stage: TeachStage;
  teach_id: string | null;
  session_id: string | null;
  skill_name: string | null;
  portal_id: string | null;
  base_url: string;
  event_count: number;
  start_response: TeachStartResponse | null;
  stop_response: TeachStopResponse | null;
  annotate_response: AnnotateResponse | null;
  error: string | null;
  unsubscribe: (() => void) | null;
  start(args: {
    skill_name: string;
    portal_id?: string;
    base_url?: string;
  }): Promise<void>;
  stop(): Promise<void>;
  annotate(useLlm: boolean): Promise<void>;
  reset(): void;
}

export const useTeachStore = create<TeachState>((set, get) => ({
  stage: "idle",
  teach_id: null,
  session_id: null,
  skill_name: null,
  portal_id: null,
  base_url: "",
  event_count: 0,
  start_response: null,
  stop_response: null,
  annotate_response: null,
  error: null,
  unsubscribe: null,

  async start(args) {
    set({
      stage: "recording",
      error: null,
      event_count: 0,
      annotate_response: null,
      stop_response: null,
      skill_name: args.skill_name,
      portal_id: args.portal_id ?? null,
      base_url: args.base_url ?? "",
    });
    try {
      const r = await bridge.teachStart(args);
      const unsub = bridge.subscribeEvents((ev: AgentEvent) => {
        if (ev.type === "teach.event_count") {
          // teach_id is on the typed event, but the union includes
          // AgentEventBase as a fallback so we read defensively.
          const count = (ev as { count?: number }).count;
          if (typeof count === "number") {
            set({ event_count: count });
          }
        }
      });
      set({
        teach_id: r.teach_id,
        session_id: r.session_id,
        start_response: r,
        unsubscribe: unsub,
      });
    } catch (e) {
      set({ stage: "error", error: (e as Error).message });
    }
  },

  async stop() {
    const { teach_id, unsubscribe } = get();
    if (!teach_id) return;
    set({ stage: "stopping" });
    try {
      const r = await bridge.teachStop({ teach_id });
      if (unsubscribe) unsubscribe();
      set({
        stage: "ready_to_record", // intermediate; UI will move to annotating
        stop_response: r,
        unsubscribe: null,
      });
    } catch (e) {
      set({ stage: "error", error: (e as Error).message });
    }
  },

  async annotate(useLlm) {
    const { session_id, skill_name } = get();
    if (!session_id || !skill_name) {
      set({
        stage: "error",
        error: "no session_id / skill_name available; cannot annotate",
      });
      return;
    }
    set({ stage: "annotating" });
    try {
      const r = await bridge.teachAnnotate({
        session_id,
        name: skill_name,
        llm: useLlm,
      });
      set({ stage: "complete", annotate_response: r });
    } catch (e) {
      set({ stage: "error", error: (e as Error).message });
    }
  },

  reset() {
    const { unsubscribe } = get();
    if (unsubscribe) unsubscribe();
    set({
      stage: "idle",
      teach_id: null,
      session_id: null,
      skill_name: null,
      portal_id: null,
      event_count: 0,
      start_response: null,
      stop_response: null,
      annotate_response: null,
      error: null,
      unsubscribe: null,
    });
  },
}));
