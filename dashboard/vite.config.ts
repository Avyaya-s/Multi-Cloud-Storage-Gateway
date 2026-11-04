import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 127.0.0.1, not localhost: on Windows localhost tries IPv6 first and is slow.
const GATEWAY = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  base: "/ui/", // the gateway serves the built app at /ui
  build: { chunkSizeWarningLimit: 700 }, // Recharts alone is ~400 kB; fine for a local dashboard
  server: {
    host: "127.0.0.1",
    port: 5173,
    // Dev server forwards API calls to the gateway, so no CORS is needed.
    proxy: {
      "/files": GATEWAY,
      "/events": GATEWAY,
      "/providers": GATEWAY,
      "/docs": GATEWAY,
      "/openapi.json": GATEWAY,
    },
  },
});
