import { defineConfig } from "vite";

export default defineConfig({
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
});
