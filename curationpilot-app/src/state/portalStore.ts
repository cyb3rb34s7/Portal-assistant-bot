import { create } from "zustand";
import { bridge } from "../host";
import type { DoctorResponse, PortalState } from "../protocol/types";

interface PortalStoreState {
  state: PortalState | null;
  doctor: DoctorResponse | null;
  loading: boolean;
  error: string | null;
  launch(targetUrl?: string): Promise<void>;
  refreshDoctor(): Promise<void>;
  close(): Promise<void>;
}

export const usePortalStore = create<PortalStoreState>((set) => ({
  state: null,
  doctor: null,
  loading: false,
  error: null,

  async launch(targetUrl) {
    set({ loading: true, error: null });
    try {
      const state = await bridge.launchPortal(targetUrl);
      set({ state, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  async refreshDoctor() {
    set({ loading: true, error: null });
    try {
      const doctor = await bridge.doctorPortal();
      set({ doctor, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  async close() {
    set({ loading: true, error: null });
    try {
      const state = await bridge.closePortal();
      set({ state, doctor: null, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },
}));
