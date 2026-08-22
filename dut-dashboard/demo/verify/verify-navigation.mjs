// The sidebar must navigate, as the product's does, and must not lie about what
// the kit contains.
//
// Both halves were defects once. Every entry used to be a button that only wrote
// a note naming the file to open, and each page's map of "what exists" was
// frozen at the moment that page was written — so a complete kit still answered
// "not in the kit yet" about a file sitting in the same directory.
import { DEMO, SCREENS, SCREEN_FILES, NO_FILE, load, click, reporter, pageErrors } from "./harness.mjs";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The role a page badges, against the roles the entries it draws actually need.
//
// The topbar badge is the product's own role pill — AppShell renders
// `pill role-<role>` for the signed-in user — and Sidebar.tsx filters the nav by
// that same role through `visibleNavItems`. So the two have to agree: a page
// badged "engineer" that still draws Upgrade Firmware (minRole admin) portrays a
// session that cannot exist. overview.html did exactly that while its fleet
// cards drew Connect, Close serial and Refresh RSSI on remote nodes, every one
// of which /api/fleet gates on admin, and no reader caught it in five rounds.
//
// The ranks and the per-screen minimums are read out of the product's source
// rather than copied here, so this check cannot outlive a change to
// navigation.ts: if either file moves or changes shape, this throws rather than
// quietly passing on an empty table.
const SRC = fileURLToPath(new URL("../../frontend/src/", import.meta.url));

function parseRoleRank() {
  const match = readFileSync(`${SRC}monitoring/AuthContext.tsx`, "utf8")
    .match(/ROLE_RANK[^=]*=\s*\{([^}]*)\}/);
  if (!match) throw new Error("no ROLE_RANK in AuthContext.tsx — has it moved?");
  return Object.fromEntries([...match[1].matchAll(/(\w+)\s*:\s*(\d+)/g)].map(m => [m[1], Number(m[2])]));
}

/** Every NAV_ITEMS entry, in sidebar order, as [label, minRole]. */
function parseNavItems() {
  const source = readFileSync(`${SRC}components/shell/navigation.ts`, "utf8");
  const items = source.slice(source.indexOf("export const NAV_ITEMS"));
  const parsed = [...items.matchAll(/label:\s*"([^"]+)"[^\n]*?minRole:\s*"(\w+)"/g)].map(m => [m[1], m[2]]);
  if (parsed.length === 0) throw new Error("no NAV_ITEMS parsed from navigation.ts — has its shape changed?");
  return parsed;
}

const RANK = parseRoleRank();
const NAV_ITEMS = parseNavItems();
const MIN_ROLE = Object.fromEntries(NAV_ITEMS);

/** What a nav entry is called, whether or not it is the current page. */
const navLabel = (el) => {
  if (el.dataset.screen) return el.dataset.screen;
  const icon = el.querySelector(".nav-icon");
  return el.textContent.replace(icon ? icon.textContent : "", "").trim();
};
const onDisk = new Set(readdirSync(DEMO).filter(f => f.endsWith(".html")));
const report = reporter();
let links = 0;
let notes = 0;
let roleChecks = 0;

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

  // Every screen badges a role, and every entry it draws must be one that role
  // can see. An absent badge is not "no claim" — the product omits the pill only
  // for the anonymous browser, and that browser is a guest.
  const badge = document.querySelector(".topbar [data-role]");
  if (!badge) {
    problems.push("no role badge in the topbar — an unbadged page reads as the anonymous guest");
  } else {
    const claimed = badge.dataset.role;
    if (!(claimed in RANK)) {
      problems.push(`badge claims "${claimed}", which is not a role in AuthContext.tsx`);
    } else {
      // Check the word the viewer reads, not just the attribute this file keys
      // off: a "engineer" pill carrying data-role="admin" would pass otherwise.
      if (!badge.textContent.trim().startsWith(claimed))
        problems.push(`badge reads "${badge.textContent.trim()}" but claims ${claimed}`);
      const drawn = [...document.querySelectorAll(".nav-item")].map(navLabel);
      for (const name of drawn) {
        const need = MIN_ROLE[name];
        if (!need) { problems.push(`${name}: no entry by that label in navigation.ts`); continue; }
        if (RANK[claimed] < RANK[need])
          problems.push(`badged ${claimed} but draws ${name}, which the product shows only to ${need}+`);
        else roleChecks++;
      }

      // ...and the other direction, which is the half a page can fail silently.
      // The sidebar is the product's, so it must hold every entry that role can
      // see, in NAV_ITEMS order — the strongest form of "showing LESS is a
      // misrepresentation too". Fleet was missing from all ten pages for two
      // releases: the product brought the nav entry back when the Fleet section
      // returned, overview.html kept only the strip, and nothing said so.
      const expected = NAV_ITEMS.filter(([, need]) => RANK[claimed] >= RANK[need]).map(([label]) => label);
      if (drawn.join(" | ") !== expected.join(" | "))
        problems.push(`sidebar is ${drawn.join(", ")}; ${claimed} sees ${expected.join(", ")}`);
      else roleChecks++;
    }
  }

  // No page may still describe the kit as partly built.
  if (/not in the kit yet|shipped so far|the other screen/.test(document.body.textContent))
    problems.push("prose still calls the kit incomplete");

  // A page that threw is not a page whose sidebar was proven to work.
  for (const error of pageErrors(window)) problems.push(`threw: ${error}`);

  report.ok(page, problems.length === 0, problems.join("; "));
}

report.finish(`${links} links, ${notes} explained gaps and ${roleChecks} role checks across ${SCREENS.length} pages`);
