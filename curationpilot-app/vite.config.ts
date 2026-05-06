import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5174,
    strictPort: true,
    // The FastAPI server (pilot serve) listens on :5177 by default.
    // Override with PILOT_API_URL=http://127.0.0.1:5179 when running a
    // second test server alongside the operator's main one. ws: true
    // lets the WebSocket /api/events upgrade through.
    proxy: {
      "/api": {
        target: process.env.PILOT_API_URL || "http://127.0.0.1:5177",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
