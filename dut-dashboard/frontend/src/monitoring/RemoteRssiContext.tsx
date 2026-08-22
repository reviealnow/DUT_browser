import { createContext, ReactNode, useCallback, useContext, useState } from "react";

import { captureRemoteRssi, getDuts, RemoteRssiResult } from "../api/rest";
import { sweepBackhauls } from "./backhaulSweep";
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
  /** The newest capture for this DUT, or what the registry already knew. Never
   *  null: a DUT nobody has measured has `captured: false`, which is a
   *  different statement from a measurement that found no backhaul. */
  get: (entry: FleetEntry) => RemoteRssiResult;
  capturing: (dutId: string) => boolean;
  /** Rejects on failure — the caller owns how that is shown. Takes the entry,
   *  not the id, so the result can be filed against the console it was actually
   *  read from (see `identityOf`). */
  refresh: (entry: FleetEntry) => Promise<RemoteRssiResult>;
  /**
   * Every DUT a capture applies to with a console open, **children before
   * roots**. Cabled DUTs included: nothing declares one standalone, and the
   * fleet's root is frequently the one on this desk.
   *
   * Not a formality: a root cannot name its own backhaul VAP from its own
   * console, and is identified from the uplink a child reports. A root captured
   * first falls back to whatever interface was configured — a silent empty
   * child list when that guess is wrong, and a cabled DUT has no guess at all.
   *
   * `role: null` is not "not a root": it is either "nobody has looked yet" or
   * "looked, and nothing yet says this DUT is in the mesh". Ordering on the
   * registry's role alone put such a DUT in the first pass only, so on a cold
   * fleet the order was whatever order the DUTs happened to be registered in.
   * Hence two passes: the second captures everything not confirmed a node,
   * including any root pass 1 only just discovered.
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
 * supported edit (the Settings card calls it "Update node"), and a cabled DUT's
 * console moves when its cable does. Keyed by id alone, this cache would hand
 * the new device the old device's role, uplink and children — including when a
 * capture started before the change lands after it.
 *
 * The value is the registry's own `console_id`, not a rule re-derived here.
 * Deriving it twice is what went wrong: the registry drops a stored capture on
 * a `backhaul_iface` edit and this cache did not, so the browser re-served a
 * reading the backend had just revoked. One rule, published, compared.
 */
function identityOf(entry: FleetEntry): string {
  return entry.backhaul.consoleId;
}

/** What the registry persisted from the last capture, before this one. */
function seed(entry: FleetEntry): RemoteRssiResult {
  return {
    dut: entry.id,
    applicable: entry.backhaul.applicable,
    captured: entry.backhaul.captured,
    console_id: entry.backhaul.consoleId,
    role: entry.backhaul.role,
    uplink: entry.backhaul.uplink,
    downlink: entry.backhaul.downlink,
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
      mark(dutId, true);
      try {
        const result = await captureRemoteRssi(dutId);
        // `captureRemoteRssi` coalesces per DUT and answers with the id it
        // captured; keying on that rather than on the id we asked for is what
        // stops a result landing on the wrong card when the fleet changes
        // under an in-flight request.
        //
        // The identity comes from the answer too, and must: the entry this was
        // called with is a snapshot from before the request, and a DUT's
        // console can change between the two. Connect on a node last read over
        // a cable is exactly that — the card is refreshed to the SSH console
        // first, then this runs with the pre-connect entry — and filing the
        // SSH reading under the cable's name made `get` reject a capture that
        // had just succeeded, leaving the card on "Not captured".
        setResults((current) =>
          new Map(current).set(result.dut, { identity: result.console_id, result }),
        );
        return result;
      } finally {
        mark(dutId, false);
      }
    },
    [mark],
  );

  const refreshAll = useCallback(
    async (entries: FleetEntry[]) => {
      // The ordering and the re-read rules live in `backhaulSweep`, with the
      // tests that hold them: they are a decision about which consoles to
      // occupy, and none of it needs React.
      await sweepBackhauls(entries, { capture: refresh, registry: getDuts });
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
