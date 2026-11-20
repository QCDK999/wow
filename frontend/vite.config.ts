import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Where the site reads live status from. On Netlify we derive it from the
// connected GitHub repo (REPOSITORY_URL is set by Netlify during builds), so no
// configuration is needed. Override with VITE_STATUS_URL if you host it elsewhere.
function defaultStatusUrl(): string {
  const repo = process.env.REPOSITORY_URL ?? "";
  const m = repo.match(/github\.com[/:]([^/]+)\/([^/.]+)/);
  const branch = process.env.STATUS_BRANCH ?? "monitor-data";
  return m ? `https://raw.githubusercontent.com/${m[1]}/${m[2]}/${branch}/status.json` : "";
}

process.env.VITE_STATUS_URL ||= defaultStatusUrl();

export default defineConfig({
  plugins: [react(), tailwindcss()],
});
