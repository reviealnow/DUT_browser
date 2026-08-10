// The sidebar must navigate, as the product's does, and must not lie about what
// the kit contains.
//
// Both halves were defects once. Every entry used to be a button that only wrote
// a note naming the file to open, and each page's map of "what exists" was
// frozen at the moment that page was written — so a complete kit still answered
// "not in the kit yet" about a file sitting in the same directory.
import { DEMO, SCREENS, SCREEN_FILES, NO_FILE, load, click, reporter } from "./harness.mjs";
import { readdirSync } from "node:fs";

const onDisk = new Set(readdirSync(DEMO).filter(f => f.endsWith(".html")));
const report = reporter();
let links = 0;
let notes = 0;

for (const page of SCREENS) {
  const window = await load(page);
  const document = window.document;
  const problems = [];

  const current = document.querySelector('.nav-item[aria-current="page"]');
  if (!current) problems.push("no entry marked as the current page");
  else if (current.tagName === "A") problems.push("the current page links to itself");

  for (const el of document.querySelectorAll(".nav-item[data-screen]")) {
    const name = el.dataset.screen;

    if (NO_FILE.has(name)) {
      if (el.tagName === "A") { problems.push(`${name}: linked, but the kit has no file for it`); continue; }
      click(window, el);
      if (!document.getElementById("navNote").textContent.includes("no file in this kit"))
        problems.push(`${name}: clicking it explains nothing`);
      else notes++;
      continue;
    }

    const want = SCREEN_FILES[name];
    if (!want) { problems.push(`${name}: not a screen this kit knows`); continue; }
    if (el.tagName !== "A") { problems.push(`${name}: still a button, so it does not navigate`); continue; }
    const href = el.getAttribute("href");
    if (href !== want) problems.push(`${name}: href is ${href}, expected ${want}`);
    else if (!onDisk.has(href)) problems.push(`${name}: links to ${href}, which does not exist`);
    else links++;
  }

  // No page may still describe the kit as partly built.
  if (/not in the kit yet|shipped so far|the other screen/.test(document.body.textContent))
    problems.push("prose still calls the kit incomplete");

  report.ok(page, problems.length === 0, problems.join("; "));
}

report.finish(`${links} links and ${notes} explained gaps across ${SCREENS.length} pages`);
