import { DutStatus } from "./useDutMonitor";

/**
 * What one DUT's pill says, from the two facts the app actually has.
 *
 * `DutStatus` alone was never enough to label a card, and the old labels said
 * more than it knew. `idle` means one thing only: **the link to the backend is
 * up and nothing tagged with this DUT has arrived for ten seconds**. It was
 * labelled "No DUT", which a reader takes as "there is no device" — so a DUT
 * with its console held open, its log written a minute ago and its shell
 * sitting quietly at a prompt was reported as absent. Raised from the bench on
 * 2026-09-16, looking at two cards that were both connected.
 *
 * Quiet and absent are different states, and the registry can tell them apart:
 * `consoleOpen` is whether a serial or SSH session is being held right now.
 *
 *   offline      the browser cannot reach the backend
 *   streaming    an event arrived < 10s ago          — the only state that moves
 *   connected    a console is held, and it is quiet
 *   no DUT       nothing is open, and nothing has arrived
 *
 * Only `streaming` carries the dot. A resting dot beside "Connected" invited
 * exactly the question this function exists to answer -- "it is connected, why
 * is the light dark?" -- so the light is now present only where it is moving,
 * and every other state is carried by its word alone.
 */
export type DutPresence = {
  label: string;
  /** The longer line under a KPI, where there is room for the distinction. */
  sub: string;
  pill: "ok" | "idle" | "danger";
  /** Whether to draw the breathing dot at all. */
  live: boolean;
};

export function dutPresence(status: DutStatus, consoleOpen: boolean): DutPresence {
  if (status === "offline") {
    return { label: "Offline", sub: "Backend not reachable", pill: "danger", live: false };
  }
  if (status === "streaming") {
    return { label: "Streaming", sub: "Receiving DUT data", pill: "ok", live: true };
  }
  if (consoleOpen) {
    // Held, and quiet. A console at a shell prompt prints nothing until
    // something asks it to, and that is not the same as having no device.
    return { label: "Connected", sub: "Console open, nothing arriving", pill: "ok", live: false };
  }
  return { label: "No DUT", sub: "Backend up, no console open", pill: "idle", live: false };
}
