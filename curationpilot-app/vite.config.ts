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
    // The FastAPI server (pilot serve) listens on :5177. Proxy /api/*
    // there so the React dev build doesn't fight CORS in the browser.
    // ws: true lets the WebSocket /api/events upgrade through.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:5177",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
