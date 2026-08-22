import { DutInfo, humanizeApiError, RemoteRssiResult, RemoteUplink } from "../api/rest";
import { FleetEntry } from "./useFleetMonitor";

/**
 * Capture every DUT's backhaul in one press, in an order that gets right
 * answers with the fewest consoles occupied.
 *
 * Lives outside the provider that calls it because it is a decision, not a
 * piece of React: given a fleet, which DUTs to read, in what order, and which
 * of them to read a second time. It reached three review rounds without a
 * single machine check behind it — every rule below was found by someone
 * reading it — so it is a plain function taking its two effects as arguments,
 * and `backhaulSweep.test.ts` walks each state through it.
 *
 * **Children before roots**, and cabled DUTs included: nothing declares a
 * cabled DUT standalone, and the fleet's root is frequently the one on this
 * desk. The order is not a formality — a root cannot name its own backhaul VAP
 * from its own console and is identified from the uplink a child reports, so a
 * root read first falls back to whatever interface was configured, which is a
 * silent empty child list when that guess is wrong, and a cabled DUT has no
 * guess at all.
 *
 * `role: null` is not "not a root": it is either "nobody has looked yet" or
 * "looked, and nothing yet says this DUT is in the mesh". Ordering on the
 * registry's role alone put such a DUT in the first pass only, so on a cold
 * fleet the order was whatever order the DUTs happened to be registered in.
 * Hence two passes, the second covering everything not confirmed a node.
 *
 * Every extra capture is two synchronous serial RPCs and another pause of that
 * DUT's sysmon parsing (`dut-dashboard/CLAUDE.md`, the RPC discipline), so a
 * DUT is read twice only where the second reading can know something the first
 * could not.
 */

/** The two things this needs from the outside, so a test can supply both. */
export type SweepEffects = {
  /** Run one capture. Rejects the way `/rssi` does. */
  capture: (entry: FleetEntry) => Promise<RemoteRssiResult>;
  /** The registry as it stands now. One HTTP GET; no console. */
  registry: () => Promise<DutInfo[]>;
};

/**
 * What a root could identify its backhaul VAP by, given this uplink — `null`
 * when the answer is "nothing".
 *
 * The backend names a root's VAP from a peer BSSID, or from an ESSID and band.
 * Both are nullable on an uplink it still calls a node's, so "there is an
 * uplink" is not the same claim as "a root could use it".
 *
 * A key rather than a boolean because the sweep has to tell a clue it already
 * had from one it has just been given: a node re-reporting the uplink the
 * registry already held teaches nothing a root read earlier did not already
 * have available to it.
 */
export function uplinkClue(uplink: RemoteUplink | null): string | null {
  if (!uplink || !(uplink.peer_mac || uplink.essid)) {
    return null;
  }
  return `${uplink.peer_mac ?? ""}|${uplink.essid ?? ""}|${uplink.radio_band ?? ""}`;
}

/**
 * Rejects with every failure joined, once the sweep has finished the rest.
 *
 * One DUT failing does not abandon the others: each is caught, and the
 * failures are reported together at the end.
 */
export async function sweepBackhauls(
  entries: FleetEntry[],
  { capture, registry }: SweepEffects,
): Promise<void> {
  const mesh = entries.filter((entry) => entry.backhaul.applicable && entry.serialOpen);
  const failures: string[] = [];
  const roles = new Map<string, "root" | "node" | null>(
    mesh.map((entry) => [entry.id, entry.backhaul.role]),
  );

  // What clue each DUT's stored uplink already offered before this sweep began.
  // A root read at step 0 could use any of these, so re-reading one is not
  // something the sweep learned and is not a reason to read anything twice.
  // One rule for both outcomes of a capture — applying it to the failures alone
  // still sent a root round again for a node that succeeded and reported what
  // the registry already held.
  const knownAtStart = new Map<string, string | null>(
    mesh.map((entry) => [entry.id, uplinkClue(entry.backhaul.uplink)]),
  );
  const isNewClue = (dutId: string, uplink: RemoteUplink | null) => {
    const clue = uplinkClue(uplink);
    return clue !== null && clue !== knownAtStart.get(dutId);
  };

  // When each DUT was read, and the earliest step after which the registry may
  // hold a clue a root can use. A root read before that saw nothing to identify
  // its backhaul VAP with; one read after it saw everything a second reading
  // could show it.
  //
  // `null` is "nothing usable was learned in this sweep at all", which is a
  // different statement from "something was, later than this root" — and only
  // the second is a reason to read a root again. A sentinel of Infinity
  // conflated them and sent a lone root round twice.
  let step = 0;
  const readAt = new Map<string, number>();
  // Every DUT this sweep has already spent a console on, successfully or not.
  // `readAt` cannot answer that — a capture that threw has no read time — and
  // pass 2 covers unclassified DUTs as well as roots, so without this a failed
  // console would be dialled twice and reported twice for one sweep.
  const attempted = new Set<string>();
  let learnedAt: number | null = null;
  const learned = (at: number) => {
    if (learnedAt === null || at < learnedAt) {
      learnedAt = at;
    }
  };

  /** Did a DUT whose capture just failed leave a usable uplink behind?
   *
   *  One registry read, no console: `/rssi` stores the uplink before it runs
   *  its second command, so a partial success is visible here. If even this
   *  fails we have learned nothing about what was learned — fall back to the
   *  assumption that costs a capture rather than the one that leaves a root
   *  blind. */
  const taughtSomething = async (dutId: string) => {
    try {
      const fresh = (await registry()).find((dut) => dut.id === dutId);
      return fresh ? isNewClue(dutId, fresh.backhaul.uplink) : false;
    } catch {
      return true;
    }
  };

  const read = async (dutId: string) => {
    const entry = mesh.find((e) => e.id === dutId)!;
    const at = step++;
    attempted.add(dutId);
    try {
      const result = await capture(entry);
      roles.set(dutId, result.role);
      readAt.set(dutId, at);
      if (isNewClue(dutId, result.uplink)) {
        learned(at);
      }
    } catch (err) {
      // A rejected capture does not mean the registry learned nothing: `/rssi`
      // stores the uplink *before* it runs the second command, so a node whose
      // `wlanconfig` failed has already taught it everything a root needs —
      // while one whose console never answered taught it nothing. Those were
      // indistinguishable from here, so this assumed the useful one and every
      // failure cost some root a second capture. They are distinguishable now:
      // the registry publishes each DUT's stored uplink, so ask it.
      if (await taughtSomething(dutId)) {
        learned(at);
      }
      // Sequential, but not fragile: a closed console or a DUT removed
      // mid-sweep costs its own reading and nothing else. Reporting them
      // together beats stopping at the first and leaving the rest stale with
      // no indication that they were never tried.
      //
      // Humanised here, not by whoever displays the aggregate: once these are
      // joined into one message the result is no longer a JSON body, so
      // `humanizeApiError` at the call site cannot unwrap it and the raw
      // `{"detail": ...}` reaches the screen — the defect #129 fixed for a
      // single failure, back again in the sweep.
      failures.push(`${dutId}: ${humanizeApiError(err)}`);
    }
  };

  // Pass 1: everything not already known to be a root — the children, and
  // anything never captured. Sequential because each is one console's
  // synchronous RPC, and because pass 2 depends on what this pass learned.
  for (const entry of mesh.filter((e) => roles.get(e.id) !== "root")) {
    await read(entry.id);
  }
  // Pass 2: everything that is not a confirmed node — the roots, and the DUTs a
  // blind pass-1 capture could not classify. One never touched in pass 1 is
  // captured here for the first time. One pass 1 did read is captured again
  // *only if* it was read before some node reported an uplink — otherwise its
  // reading already stands, and a second would be two more serial RPCs and
  // another pause of that DUT's sysmon parsing for a byte-identical answer. A
  // lone root, with no node in the fleet to learn from, is the clearest case:
  // there is nothing a second reading could know that the first did not.
  //
  // Unclassified DUTs belong here for the reason roots do, and more so: a
  // cabled DUT declares nothing, so "no uplink and nothing names my VAPs" is
  // exactly the answer a blind read gives — and the node that would name them
  // may have been captured one step later in the same sweep.
  for (const entry of mesh.filter((e) => roles.get(e.id) !== "node")) {
    const at = readAt.get(entry.id);
    const readBlind = at !== undefined && learnedAt !== null && at < learnedAt;
    if (!attempted.has(entry.id) || readBlind) {
      await read(entry.id);
    }
  }

  if (failures.length > 0) {
    throw new Error(failures.join("; "));
  }
}
