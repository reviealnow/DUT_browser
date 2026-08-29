import { describe, expect, it } from "vitest";

import { applySnapshotDelta, SnapshotPayload } from "./websocket";

/**
 * Rebuilding a snapshot from a delta, and the field that is easiest to lose.
 *
 * This function returns a fresh object built from a fixed set of keys, so
 * anything not named in it is dropped rather than carried. It is also a mirror:
 * `_reconstruct` in snapshot_store.py does the same job on the backend, and the
 * two disagreeing means the reading a card renders live differs from the one it
 * backfills after a reload.
 *
 * `device_id` is the field that matters here. It says which unit a reading was
 * measured on -- the only thing that separates a fresh 420E's CPU figures from
 * a departed 420E's -- and a delta never carries one, because the update the
 * chain started from did. Dropped in the rebuild, it would vanish from every
 * reading that arrived as a delta, which on a live console is most of them, and
 * the card would fall silent about exactly the sessions it exists to watch.
 */

function base(over: Partial<SnapshotPayload> = {}): SnapshotPayload {
  return {
    test_count: 1,
    device_ts: "T1",
    device_id: "AP6420E-PB1005QPCFVFMA8",
    cpu: { "0": { idle: 80 } as never },
    memory: { MemAvailable: 475472 } as never,
    wifi_clients: {},
    ...over,
  };
}

describe("rebuilding a snapshot from a delta", () => {
  it("keeps the unit the reading was measured on", () => {
    const next = applySnapshotDelta(base(), { cpu: { "1": { idle: 90 } as never } });
    expect(next.device_id).toBe("AP6420E-PB1005QPCFVFMA8");
  });

  it("keeps it across a run of deltas, not just the first", () => {
    let snapshot = base();
    for (const idle of [70, 60, 50]) {
      snapshot = applySnapshotDelta(snapshot, { cpu: { "0": { idle } as never } });
    }
    expect(snapshot.device_id).toBe("AP6420E-PB1005QPCFVFMA8");
    expect(snapshot.cpu["0"].idle).toBe(50);
  });

  it("leaves an unstamped reading unstamped rather than inventing one", () => {
    // Every snapshot recorded before the field existed is one of these.
    const next = applySnapshotDelta(base({ device_id: undefined }), { test_count: 2 });
    expect(next.device_id).toBeUndefined();
  });

  it("still merges the fields it always did", () => {
    const next = applySnapshotDelta(base(), {
      device_ts: "T2",
      cpu: { "1": { idle: 90 } as never },
      memory: { MemAvailable: 470000 } as never,
    });
    expect(next.device_ts).toBe("T2");
    expect(Object.keys(next.cpu).sort()).toEqual(["0", "1"]);
    expect(next.memory).toEqual({ MemAvailable: 470000 });
  });
});
