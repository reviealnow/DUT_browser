/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "live" (default) talks to the FastAPI backend; "demo" replays bundled data. */
  readonly VITE_APP_MODE?: "live" | "demo";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
