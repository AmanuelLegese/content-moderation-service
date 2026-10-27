import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // `npm run dev`: forward /api/* to the API on :8000 (the same job nginx does in Docker),
  // so the code always calls relative /api URLs and never needs CORS.
  server: { proxy: { "/api": { target: "http://localhost:8000", rewrite: (p) => p.replace(/^\/api/, "") } } },
  // Vitest runs components in jsdom, a simulated browser DOM.
  test: { environment: "jsdom" },
  // `as never`: the `test` key comes from Vitest and isn't in Vite's own config type.
} as never);
