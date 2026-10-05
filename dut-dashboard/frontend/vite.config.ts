import { defineConfig } from "vite";

// Where the Demo Mode build is served from: the `app/` folder of the project's
// GitHub Pages site, next to the static demo kit at the site root (see
// .github/workflows/demo-pages.yml). Overridable for a fork or another host.
const DEMO_BASE = process.env.DEMO_BASE ?? "/DUT_browser/app/";

// `--mode demo` (npm run dev:demo / build:demo) loads .env.demo, which sets
// VITE_APP_MODE=demo; every other mode is the live, backend-served app at `/`.
export default defineConfig(({ mode }) => ({
  base: mode === "demo" ? DEMO_BASE : "/",
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Split large third-party libs into their own chunks so the initial
        // bundle stays small: react-vendor loads on first paint (cacheable),
        // while codemirror and xterm ride along with the lazy chunks that use
        // them (Serial Console / Terminal) instead of the main bundle.
        manualChunks: {
          "react-vendor": ["react", "react-dom"],
          codemirror: [
            "@codemirror/view",
            "@codemirror/state",
            "@codemirror/commands",
            "@uiw/react-codemirror",
            "@replit/codemirror-vim",
          ],
          xterm: ["@xterm/xterm", "@xterm/addon-fit"],
        },
      },
    },
  },
}));
