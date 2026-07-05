import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Proxy /api/* to the Flask backend so the browser only talks to Vite in dev
// and the frontend never needs the backend's absolute URL (or any API key).
export default defineConfig({
  // GitHub Pages serves the site under /IsItVegan/ — the deploy workflow
  // sets VITE_BASE; local dev and builds default to /.
  base: process.env.VITE_BASE || "/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    allowedHosts: ["nutty-stuffy-snuff.ngrok-free.dev"],
    proxy: {
      "/api": "http://127.0.0.1:5000",
    },
  },
});
