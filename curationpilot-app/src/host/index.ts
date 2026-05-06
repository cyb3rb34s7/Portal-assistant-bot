// Picks the right HostBridge implementation. v1 is web-only; v2
// will swap in bridge.electron based on a build flag (e.g.
// import.meta.env.VITE_HOST_TARGET).

export { bridge } from "./bridge.web";
export type { HostBridge } from "./bridge";
