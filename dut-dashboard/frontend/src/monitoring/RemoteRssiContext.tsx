import { createContext, ReactNode, useCallback, useContext, useState } from "react";

import { captureRemoteRssi, humanizeApiError, RemoteRssiResult } from "../api/rest";
import { FleetEntry } from "./useFleetMonitor";

/**
 * The backhaul capture, held once for every view that shows it.
 *
 * A provider rather than a hook each view calls: the Overview strip and the
 * Fleet section render the same cards, and a hook instance per view is a state
 * per view. They are never mounted together, so the visible cost is a race —
 * capture on the strip, switch to Fleet before it lands, and the result arrives
 * at a hook nobody is rendering while the section seeds itself from whatever
 * `/api/duts` answered a moment earlier. (The backend does persist a capture to
 * the registry, so the seed is usually current; "usually" is the bug.)
 *
 * Nothing here polls. A capture occupies that DUT's serial console — see the
 * RPC discipline in `dut-dashboard/CLAUDE.md` — so it runs only when asked.
 */
export type RemoteRssiState = {
  /** The newest capture for this DUT, or what the registry already knew. */
  get: (entry: FleetEntry) => RemoteRssiResult | null;
  capturing: (dutId: string) => boolean;
  /** Rejects on failure — the caller owns how that is shown. Takes the entry,
   *  not the id, so the result can be filed against the console it was actually
   *  read from (see `identityOf`). */
  refresh: (entry: FleetEntry) => Promise<RemoteRssiResult>;
  /**
   * Every mesh node with a console open, **children before roots**.
   *
   * Not a formality: a root cannot name its own backhaul VAP from its own
   * console, and is identified from the uplink a child reports. A root captured
   * first falls back to whatever interface was configured, which is a silent
   * empty child list when that guess is wrong.
   *
   * A DUT that has never been captured has `role: null`, which is not "not a
   * root" — it is "nobody knows yet". Ordering on the registry's role alone put
   * an unclassified root in the first pass, so on a cold fleet the order was
   * whatever order the DUTs happened to be registered in. Hence two passes: the
   * second captures the roots, including any that pass 1 only just discovered.
   *
   * A root is captured **twice only when the first reading could not be
   * trusted** — when it ran before any node in this sweep had reported an
   * uplink. The backend identifies a root's backhaul VAP from uplinks other
   * DUTs already persisted (`_known_uplinks`), so a root captured after a
   * successful node capture has already seen everything a second run would
   * show it. Re-capturing it anyway costs two more synchronous serial RPCs and
   * pauses that DUT's sysmon parsing for nothing — and for a lone root, with no
   * node to learn from at all, the second reading is byte for byte the first.
   *
   * One DUT failing does not abandon the rest: each is caught, and the failures
   * are reported together at the end.
   */
  refreshAll: (entries: FleetEntry[]) => Promise<void>;
};

/**
 * Which console a reading came from — not just which DUT id.
 *
 * An id is re-usable: re-pointing a node at another Pi under the same id is a
 * supported edit (the Settings card calls it "Update node"). Keyed by id alone,
 * this cache would hand the new device the old device's role, uplink and
 * children — including when a capture started before the change lands after it.
 *
 * The value is the registry's own `console_id`, not a rule re-derived here.
 * Deriving it twice is what went wrong: the registry drops a stored capture on
 * a `backhaul_iface` edit and this cache did not, so the browser re-served a
 * reading the backend had just revoked. One rule, published, compared.
 */
function identityOf(entry: FleetEntry): string {
  return entry.remote ? entry.remote.consoleId : "local";
}

/** What the registry persisted from the last capture, before this one. */
function seed(entry: FleetEntry): RemoteRssiResult | null {
  if (!entry.remote) {
    return null;
  }
  return {
    dut: entry.id,
    applicable: entry.remote.isMesh,
    role: entry.remote.role,
    uplink: entry.remote.uplink,
    downlink: entry.remote.downlink,
  };
}

const RemoteRssiContext = createContext<RemoteRssiState | null>(null);

/** A reading, and the console it was read from. */
type Held = { identity: string; result: RemoteRssiResult };

export function RemoteRssiProvider({ children }: { children: ReactNode }) {
  // Keyed by DUT id — one entry per id, so re-registering overwrites rather
  // than accumulating, and the map cannot outgrow the registry's own limit.
  const [results, setResults] = useState<Map<string, Held>>(new Map());
  const [inflight, setInflight] = useState<Set<string>>(new Set());

  const mark = useCallback((dutId: string, busy: boolean) => {
    setInflight((current) => {
      const next = new Set(current);
      if (busy) {
        next.add(dutId);
      } else {
        next.delete(dutId);
      }
      return next;
    });
  }, []);

  const refresh = useCallback(
    async (entry: FleetEntry) => {
      const dutId = entry.id;
      const identity = identityOf(entry);
      mark(dutId, true);
      try {
        const result = await captureRemoteRssi(dutId);
        // `captureRemoteRssi` coalesces per DUT and answers with the id it
        // captured; keying on that rather than on the id we asked for is what
        // stops a result landing on the wrong card when the fleet changes
        // under an in-flight request. The identity is the one the capture was
        // *started* against, so a reading that lands after the node was
        // re-pointed is filed against the console it actually came from and
        // simply stops matching.
        setResults((current) => new Map(current).set(result.dut, { identity, result }));
        return result;
      } finally {
        mark(dutId, false);
      }
    },
    [mark],
  );

  const refreshAll = useCallback(
    async (entries: FleetEntry[]) => {
      const mesh = entries.filter((entry) => entry.remote?.isMesh && entry.serialOpen);
      const failures: string[] = [];
      const roles = new Map<string, "root" | "node" | null>(
        mesh.map((entry) => [entry.id, entry.remote!.role]),
      );

      // When each DUT was read, and the earliest step after which the registry
      // may hold a clue a root can use. A root read before that saw nothing to
      // identify its backhaul VAP with; one read after it saw everything a
      // second reading could show it.
      //
      // `null` is "nothing usable was learned in this sweep at all", which is a
      // different statement from "something was, later than this root" — and
      // only the second is a reason to read a root again. A sentinel of
      // Infinity conflated them and sent a lone root round twice.
      let step = 0;
      const readAt = new Map<string, number>();
      let learnedAt: number | null = null;
      const learned = (at: number) => {
        if (learnedAt === null || at < learnedAt) {
          learnedAt = at;
        }
      };

      const capture = async (dutId: string) => {
        const entry = mesh.find((e) => e.id === dutId)!;
        const at = step++;
        const wasKnownRoot = roles.get(dutId) === "root";
        try {
          const result = await refresh(entry);
          roles.set(dutId, result.role);
          readAt.set(dutId, at);
          // `role: "node"` is not the same claim. The backend identifies a
          // root's backhaul VAP from a peer BSSID, or from an ESSID and band —
          // and both of those are nullable on an uplink it still calls a node's.
          // A node that reported neither taught the registry nothing.
          const uplink = result.uplink;
          if (result.role === "node" && uplink && (uplink.peer_mac || uplink.essid)) {
            learned(at);
          }
        } catch (err) {
          // A rejected capture does not mean the registry learned nothing:
          // `/rssi` stores the uplink *before* it runs the second command, so a
          // node whose `wlanconfig` failed has already taught it everything a
          // root needs. We cannot tell that case from a console that never
          // answered, so assume the useful one — the cost of being wrong is one
          // extra capture, and the cost of the other assumption is a root left
          // holding a downlink read while it was still blind. A root is the
          // exception: its capture has no uplink to store, so a failed one
          // cannot have taught the registry anything.
          if (!wasKnownRoot) {
            learned(at);
          }
          // Sequential, but not fragile: a closed console or a DUT removed
          // mid-sweep costs its own reading and nothing else. Reporting them
          // together beats stopping at the first and leaving the rest stale
          // with no indication that they were never tried.
          // Humanised here, not by whoever displays the aggregate: once these
          // are joined into one message the result is no longer a JSON body,
          // so `humanizeApiError` at the call site cannot unwrap it and the raw
          // `{"detail": ...}` reaches the screen — the defect #129 fixed for a
          // single failure, back again in the sweep.
          failures.push(`${dutId}: ${humanizeApiError(err)}`);
        }
      };

      // Pass 1: everything not already known to be a root — the children, and
      // anything never captured. Sequential because each is one console's
      // synchronous RPC, and because pass 2 depends on what this pass learned.
      for (const entry of mesh.filter((e) => roles.get(e.id) !== "root")) {
        await capture(entry.id);
      }
      // Pass 2: the roots. One that pass 1 never touched is captured here for
      // the first time. One that pass 1 discovered is captured again *only if*
      // it was read before some node reported an uplink — otherwise its reading
      // already stands, and a second would be two more serial RPCs and another
      // pause of that DUT's sysmon parsing for a byte-identical answer. A lone
      // root, with no node in the fleet to learn from, is the clearest case:
      // there is nothing a second reading could know that the first did not.
      for (const entry of mesh.filter((e) => roles.get(e.id) === "root")) {
        const at = readAt.get(entry.id);
        const readBlind = at !== undefined && learnedAt !== null && at < learnedAt;
        if (at === undefined || readBlind) {
          await capture(entry.id);
        }
      }

      if (failures.length > 0) {
        throw new Error(failures.join("; "));
      }
    },
    [refresh],
  );

  const get = useCallback(
    (entry: FleetEntry) => {
      const held = results.get(entry.id);
      // A reading from a different console than this card now names is not this
      // card's reading, however recent it is.
      return held && held.identity === identityOf(entry) ? held.result : seed(entry);
    },
    [results],
  );

  const capturing = useCallback((dutId: string) => inflight.has(dutId), [inflight]);

  return (
    <RemoteRssiContext.Provider value={{ get, capturing, refresh, refreshAll }}>
      {children}
    </RemoteRssiContext.Provider>
  );
}

export function useRemoteRssi(): RemoteRssiState {
  const ctx = useContext(RemoteRssiContext);
  if (ctx === null) {
    throw new Error("useRemoteRssi must be used within a RemoteRssiProvider");
  }
  return ctx;
}
