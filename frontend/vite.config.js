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
    // Listen on the LAN so a phone on the home network / WireGuard tunnel
    // can open the dev app at http://192.168.1.50:5173. The backend stays
    // loopback-only; the /api proxy below runs server-side on this machine.
    host: true,
    allowedHosts: ["nutty-stuffy-snuff.ngrok-free.dev"],
    proxy: {
      "/api": "http://127.0.0.1:5000",
    },
  },
});
