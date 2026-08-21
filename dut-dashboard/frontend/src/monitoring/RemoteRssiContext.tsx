import { createContext, ReactNode, useCallback, useContext, useState } from "react";

import {
  captureRemoteRssi,
  getDuts,
  humanizeApiError,
  RemoteRssiResult,
  RemoteUplink,
} from "../api/rest";
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

/**
 * Whether an uplink carries something a root can be identified by.
 *
 * The backend names a root's backhaul VAP from a peer BSSID, or from an ESSID
 * and band. Both are nullable on an uplink it still calls a node's, so "there
 * is an uplink" is not the same claim as "a root could use it".
 */
function usableUplink(uplink: RemoteUplink | null): boolean {
  return !!uplink && !!(uplink.peer_mac || uplink.essid);
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
      const mesh = entries.filter((entry) => entry.backhaul.applicable && entry.serialOpen);
      const failures: string[] = [];
      const roles = new Map<string, "root" | "node" | null>(
        mesh.map((entry) => [entry.id, entry.backhaul.role]),
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
      // Which DUTs already had a usable uplink stored before this sweep began.
      // A root read at step 0 could use those, so they are not something the
      // sweep "learned" and are not a reason to read anything twice.
      const knownAtStart = new Map<string, boolean>(
        mesh.map((entry) => [entry.id, usableUplink(entry.backhaul.uplink)]),
      );
      let step = 0;
      const readAt = new Map<string, number>();
      // Every DUT this sweep has already spent a console on, successfully or
      // not. `readAt` cannot answer that — a capture that threw has no read
      // time — and pass 2 now covers unclassified DUTs as well as roots, so
      // without this a failed console would be dialled twice and reported
      // twice for one sweep.
      const attempted = new Set<string>();
      let learnedAt: number | null = null;
      const learned = (at: number) => {
        if (learnedAt === null || at < learnedAt) {
          learnedAt = at;
        }
      };

      /** Did a DUT whose capture just failed leave a usable uplink behind?
       *
       *  One registry read, no console: `/rssi` stores the uplink before it
       *  runs its second command, so a partial success is visible here. If even
       *  this fails we have learned nothing about what was learned — fall back
       *  to the assumption that costs a capture rather than the one that leaves
       *  a root blind. */
      const taughtSomething = async (dutId: string) => {
        try {
          const fresh = (await getDuts()).find((dut) => dut.id === dutId);
          return fresh ? usableUplink(fresh.backhaul.uplink) : false;
        } catch {
          return true;
        }
      };

      const capture = async (dutId: string) => {
        const entry = mesh.find((e) => e.id === dutId)!;
        const at = step++;
        attempted.add(dutId);
        try {
          const result = await refresh(entry);
          roles.set(dutId, result.role);
          readAt.set(dutId, at);
          // `role: "node"` is not the same claim. The backend identifies a
          // root's backhaul VAP from a peer BSSID, or from an ESSID and band —
          // and both of those are nullable on an uplink it still calls a node's.
          // A node that reported neither taught the registry nothing.
          if (usableUplink(result.uplink)) {
            learned(at);
          }
        } catch (err) {
          // A rejected capture does not mean the registry learned nothing:
          // `/rssi` stores the uplink *before* it runs the second command, so a
          // node whose `wlanconfig` failed has already taught it everything a
          // root needs — while one whose console never answered taught it
          // nothing. Those were indistinguishable from here, so this assumed
          // the useful one and every failure cost some root a second capture.
          //
          // They are distinguishable now: the registry publishes each DUT's
          // stored uplink for every DUT, so ask it. That is one HTTP GET and no
          // console at all — the reason to bother is that the alternative is
          // two synchronous serial RPCs and another pause of a DUT's sysmon
          // parsing, for a clue that may not exist. Only a *new* uplink counts:
          // one this DUT already had before the sweep was available to every
          // root read in it, including those read first.
          if (!knownAtStart.get(dutId) && (await taughtSomething(dutId))) {
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
      // Pass 2: everything that is not a confirmed node — the roots, and the
      // DUTs a blind pass-1 capture could not classify. One never touched in
      // pass 1 is captured here for the first time. One pass 1 did read is
      // captured again *only if* it was read before some node reported an
      // uplink — otherwise its reading already stands, and a second would be
      // two more serial RPCs and another pause of that DUT's sysmon parsing
      // for a byte-identical answer. A lone root, with no node in the fleet to
      // learn from, is the clearest case: there is nothing a second reading
      // could know that the first did not.
      //
      // Unclassified DUTs belong here for the reason roots do, and more so: a
      // cabled DUT declares nothing, so "no uplink and nothing names my VAPs"
      // is exactly the answer a blind read gives — and the node that would
      // name them may have been captured one step later in the same sweep.
      for (const entry of mesh.filter((e) => roles.get(e.id) !== "node")) {
        const at = readAt.get(entry.id);
        const readBlind = at !== undefined && learnedAt !== null && at < learnedAt;
        if (!attempted.has(entry.id) || readBlind) {
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
