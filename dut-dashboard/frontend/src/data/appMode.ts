/**
 * Which world this build talks to. Read once, here, so the rest of the app
 * never inspects `import.meta.env` itself.
 *
 * - `live` (the default): the FastAPI backend on the same origin, over REST
 *   and the one shared `/ws`. Anything other than the exact string "demo"
 *   lands here, so a missing or misspelt variable can never silently turn a
 *   bench build into a demo.
 * - `demo`: the public GitHub Pages build. Telemetry is replayed from files
 *   bundled under `public/demo/`, and no request ever leaves for a backend.
 *
 * Set with `VITE_APP_MODE` (see `.env.demo` and the `*:demo` npm scripts).
 */
export type AppMode = "live" | "demo";

export const APP_MODE: AppMode = import.meta.env.VITE_APP_MODE === "demo" ? "demo" : "live";

export const IS_DEMO = APP_MODE === "demo";

/** The copy every refused demo action shows, so it reads the same everywhere. */
export const DEMO_UNAVAILABLE =
  "Not available in Demo Mode — this needs a real DUT and the local backend.";
