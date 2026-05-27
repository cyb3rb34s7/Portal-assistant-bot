import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mockApiPlugin } from "./mock_backend.js";

// The mock backend lives in mock_backend.js. It's a real Vite middleware
// that handles /api/* with in-memory state, bearer auth, real latency
// (2-3s on heavy endpoints), and Idempotency-Key dedupe. Nothing about
// the data is hardcoded for any specific test scenario -- it's a
// generic enterprise-shaped backend the portal pages drive against.
export default defineConfig({
  plugins: [react(), mockApiPlugin()],
  server: {
    port: 5188,
    strictPort: true,
  },
});
