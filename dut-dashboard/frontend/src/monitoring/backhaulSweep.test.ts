import { describe, expect, it } from "vitest";

import { DutInfo, RemoteRssiResult, RemoteUplink } from "../api/rest";
import { sweepBackhauls, uplinkClue } from "./backhaulSweep";
import { FleetEntry } from "./useFleetMonitor";

/**
 * What "Capture all" costs, per DUT, in every state a fleet can be in.
 *
 * The unit of waste is one extra capture: two synchronous serial RPCs and
 * another pause of that DUT's sysmon parsing. The unit of harm is a root left
 * blind — read before any node reported the uplink that names its backhaul VAP,
 * and never read again. Both are counted here, because this logic reached three
 * review rounds with nothing but reading behind it and each round changed it.
 */

const UPLINK: RemoteUplink = {
  iface: "ath15",
  rssi: -37,
  snr: 55,
  rssi_band: "near",
  radio_band: "5GHz",
  essid: "backhaul",
  peer_mac: "ce:4f:86:95:ce:e5",
};

function entry(
  id: string,
  {
    role = null,
    uplink = null,
    applicable = true,
    serialOpen = true,
  }: {
    role?: "root" | "node" | null;
    uplink?: RemoteUplink | null;
    applicable?: boolean;
    serialOpen?: boolean;
  } = {},
): FleetEntry {
  return {
    id,
    label: id,
    status: "streaming",
    serialOpen,
    lastSerial: null,
    remote: null,
    backhaul: {
      applicable,
      captured: role !== null,
      consoleId: `console-${id}`,
      role,
      uplink,
      downlink: null,
    },
    cpuBusyPct: null,
    coreCount: 0,
    crashCount: 0,
    lastSnapshotTs: null,
    lastEventAgeSec: null,
  };
}

function result(dut: string, role: "root" | "node" | null, uplink: RemoteUplink | null = null): RemoteRssiResult {
  return { dut, applicable: true, captured: true, console_id: `console-${dut}`, role, uplink, downlink: null };
}

/** A registry answer carrying one DUT's stored uplink and nothing else. */
function registryWith(uplinks: Record<string, RemoteUplink | null>): () => Promise<DutInfo[]> {
  return async () =>
    Object.entries(uplinks).map(([id, uplink]) => ({
      id,
      label: id,
      mode: "serial" as const,
      serial_open: true,
      log_path: null,
      removable: true,
      last_serial: null,
      remote: null,
      backhaul: {
        applicable: true,
        captured: uplink !== null,
        console_id: `console-${id}`,
        role: uplink ? ("node" as const) : null,
        uplink,
        downlink: null,
      },
    }));
}

/** Runs a sweep, recording the order consoles were occupied in. */
function record(
  entries: FleetEntry[],
  answer: (id: string, nth: number) => Promise<RemoteRssiResult>,
  registry: () => Promise<DutInfo[]> = registryWith({}),
) {
  const reads: string[] = [];
  const counts = new Map<string, number>();
  const capture = (target: FleetEntry) => {
    reads.push(target.id);
    const nth = (counts.get(target.id) ?? 0) + 1;
    counts.set(target.id, nth);
    return answer(target.id, nth);
  };
  return { reads, run: () => sweepBackhauls(entries, { capture, registry }) };
}

describe("which DUTs a sweep reads, and how many times", () => {
  it("reads a confirmed node once", async () => {
    const { reads, run } = record([entry("node1", { role: "node", uplink: UPLINK })], async (id) =>
      result(id, "node", UPLINK),
    );
    await run();
    expect(reads).toEqual(["node1"]);
  });

  it("reads a known root once, in the second pass", async () => {
    const { reads, run } = record(
      [entry("root1", { role: "root" }), entry("node1", { role: "node", uplink: UPLINK })],
      async (id) => (id === "root1" ? result(id, "root") : result(id, "node", UPLINK)),
    );
    await run();
    // Children before roots: the root is identified from what the node reports.
    expect(reads).toEqual(["node1", "root1"]);
  });

  it("does not read a lone root twice — nothing could have taught it anything", async () => {
    const { reads, run } = record([entry("root1")], async (id) => result(id, "root"));
    await run();
    expect(reads).toEqual(["root1"]);
  });

  it("reads a blind DUT again when a node later reports a new clue", async () => {
    const entries = [entry("maybe-root"), entry("node1")];
    const { reads, run } = record(entries, async (id, nth) => {
      if (id === "node1") {
        return result(id, "node", UPLINK);
      }
      // Read blind, this cabled DUT has no parent and nothing yet ties it to a
      // mesh: `role: null`, which is neither "root" nor "nobody looked". Pass 2
      // covering only the *roots* would drop it here — the reading it needs
      // arrives one step later, from node1. Answering "root" for this first
      // read is what made an earlier version of this test pass against that
      // bug, because the roles map then carried it into pass 2 anyway.
      return nth === 1 ? result(id, null) : result(id, "root");
    });
    await run();
    expect(reads).toEqual(["maybe-root", "node1", "maybe-root"]);
  });

  it("does not read it again when the node only repeats a clue the registry had", async () => {
    const entries = [entry("maybe-root"), entry("node1", { role: "node", uplink: UPLINK })];
    const { reads, run } = record(entries, async (id) =>
      id === "node1" ? result(id, "node", UPLINK) : result(id, null),
    );
    await run();
    // node1's uplink was already stored before the sweep: every read in this
    // sweep could use it, including the first.
    expect(reads).toEqual(["maybe-root", "node1"]);
  });

  it("skips a DUT with no console open, and one no capture applies to", async () => {
    const entries = [
      entry("closed", { serialOpen: false }),
      entry("standalone", { applicable: false }),
      entry("node1", { role: "node", uplink: UPLINK }),
    ];
    const { reads, run } = record(entries, async (id) => result(id, "node", UPLINK));
    await run();
    expect(reads).toEqual(["node1"]);
  });
});

describe("what a failed capture costs the rest of the sweep", () => {
  const boom = () => Promise.reject(new Error("Serial port is not open"));

  it("does not dial a failed console twice in one sweep", async () => {
    const { reads, run } = record([entry("dead")], boom);
    await expect(run()).rejects.toThrow(/dead: /);
    expect(reads).toEqual(["dead"]);
  });

  it("still reports every other DUT's failure, not just the first", async () => {
    const { reads, run } = record([entry("dead1"), entry("dead2")], boom);
    await expect(run()).rejects.toThrow(/dead1: .*dead2: /s);
    expect(reads).toEqual(["dead1", "dead2"]);
  });

  it("re-reads an earlier blind DUT when the failure left a new clue behind", async () => {
    // `/rssi` stores the uplink before its second command, so a capture that
    // failed at `wlanconfig` has already taught the registry what a root needs.
    const entries = [entry("maybe-root"), entry("partial")];
    const { reads, run } = record(
      entries,
      async (id) => (id === "partial" ? boom() : result(id, "root")),
      registryWith({ partial: UPLINK }),
    );
    await expect(run()).rejects.toThrow(/partial: /);
    expect(reads).toEqual(["maybe-root", "partial", "maybe-root"]);
  });

  it("does not re-read anything when the failure left nothing behind", async () => {
    const entries = [entry("maybe-root"), entry("dead")];
    const { reads, run } = record(
      entries,
      async (id) => (id === "dead" ? boom() : result(id, "root")),
      registryWith({ dead: null }),
    );
    await expect(run()).rejects.toThrow(/dead: /);
    expect(reads).toEqual(["maybe-root", "dead"]);
  });

  it("assumes the useful answer when the registry cannot be read either", async () => {
    // Unknowable is not the same as "nothing was learned". One extra capture
    // costs a console; the other assumption leaves a root blind.
    const entries = [entry("maybe-root"), entry("dead")];
    const { reads, run } = record(
      entries,
      async (id) => (id === "dead" ? boom() : result(id, "root")),
      () => Promise.reject(new Error("backend unreachable")),
    );
    await expect(run()).rejects.toThrow(/dead: /);
    expect(reads).toEqual(["maybe-root", "dead", "maybe-root"]);
  });
});

describe("what counts as a clue a root can use", () => {
  it("is nothing when the uplink names neither a peer nor an SSID", () => {
    expect(uplinkClue(null)).toBeNull();
    expect(uplinkClue({ ...UPLINK, peer_mac: null, essid: null })).toBeNull();
  });

  it("distinguishes a re-association from the same reading twice", () => {
    expect(uplinkClue(UPLINK)).toEqual(uplinkClue({ ...UPLINK, rssi: -60 }));
    expect(uplinkClue(UPLINK)).not.toEqual(uplinkClue({ ...UPLINK, peer_mac: "aa:bb:cc:dd:ee:ff" }));
  });
});
