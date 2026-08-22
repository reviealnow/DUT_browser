// Loading a demo page into a real DOM, and the tiny assertion helper the two
// verifiers share.
//
// Why a DOM at all: these pages are markup plus a script, and every review
// finding this kit has had was about what a control *does*, not about how the
// source reads. Checking the source is how a `prompt()` where the product has an
// inline editor, or a Send that quietly appended invented text, both survived
// being read. So the verifiers load the shipped file and click its own controls.
//
// Why jsdom rather than a browser: the pages are opened from disk, and `file://`
// is blocked by both the in-app preview surface (it renders a static snapshot)
// and Playwright (the scheme is refused outright). jsdom has no such policy, and
// what these checks need is the DOM and the event loop, not a renderer.
import { JSDOM, VirtualConsole } from "jsdom";
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const DEMO = fileURLToPath(new URL("..", import.meta.url));
export const PAGES = readdirSync(DEMO).filter(f => f.endsWith(".html")).sort();
/** Every page except the front door, which has a tile grid and no sidebar. */
export const SCREENS = PAGES.filter(p => p !== "index.html");

/** Screen name in the sidebar -> the file it must open. */
export const SCREEN_FILES = {
  "Overview": "overview.html", "Fleet": "fleet.html",
  "Site Survey": "site-survey.html",
  "Wi-Fi Clients": "wifi-clients.html", "CPU / Memory": "cpu-memory.html",
  "SSID Capability": "ssid-capability.html", "Downloads": "downloads.html",
  "Serial Console": "serial-console.html", "Files": "files.html",
  "Bulletin": "bulletin.html", "Upgrade Firmware": "firmware.html",
};
/** Real product screens the kit has no file for; they explain themselves. */
export const NO_FILE = new Set(["Logs / Crash Events", "Settings"]);

/**
 * Load a page and record anything it throws.
 *
 * Without this the harness is blind to the loudest failure there is: a page
 * whose script dies on load still answers questions about its DOM, so the
 * assertions pass over a half-built page and report green. Verified by
 * injecting a call to an undefined function after boot — 29 assertions passed
 * and nothing said the page had thrown.
 *
 * `jsdomError` carries uncaught exceptions and script-loading failures;
 * `console.error` catches what a page reports about itself.
 */
export async function load(page) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", error => errors.push(error.message || String(error)));
  virtualConsole.on("error", (...args) => errors.push(args.join(" ")));

  const dom = await JSDOM.fromFile(`${DEMO}${page}`, {
    runScripts: "dangerously", pretendToBeVisual: true,
    url: `http://localhost/${page}`, virtualConsole,
  });
  const { window } = dom;
  window.__errors = errors;
  // jsdom implements neither, and the pages only need them to exist.
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  await new Promise(resolve => window.addEventListener("load", resolve, { once: true }));
  return window;
}

export const click = (window, el) =>
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

/** Everything the page threw or logged as an error, load and clicks alike. */
export const pageErrors = (window) => window.__errors;

export function reporter() {
  let passed = 0;
  const failures = [];
  return {
    ok(name, condition, detail = "") {
      if (condition) { passed++; console.log(`  PASS  ${name}`); }
      else { failures.push(name); console.log(`  FAIL  ${name}${detail ? `  — ${detail}` : ""}`); }
    },
    section(title) { console.log(`\n${title}`); },
    finish(what) {
      console.log(`\n${passed} passed, ${failures.length} failed${what ? ` — ${what}` : ""}`);
      if (failures.length) {
        console.log(failures.map(f => `  failed: ${f}`).join("\n"));
        process.exit(1);
      }
    },
  };
}
