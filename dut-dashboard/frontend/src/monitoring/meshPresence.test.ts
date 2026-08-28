import { describe, expect, it } from "vitest";

import type { MeshMember, MeshProbe, MeshTopology } from "../api/rest";
import { meshPresence } from "./MeshTopologyContext";
import type { FleetEntry } from "./useFleetMonitor";

/**
 * Whether a mesh exists here at all — and the third answer that keeps it honest.
 *
 * The Fleet page hides DUTs on the strength of this, so folding "we could not
 * tell" into "no" would mean a busy serial line makes half the bench disappear.
 * The mesh probe already separates those three states for the same reason, and
 * this must not undo that one level up.
 *
 * The "none" branch is not an assumption either. AP6840E-PD1005VMG3KJH9C
 * answers `{"mesh_info_list":[],"total_size":0,"error_code":0}` on this bench
 * when it is in no mesh — measured 2026-08-28 over its own console. A device
 * with nothing to report uses a normal reply and an empty list; an error code
 * produces `mesh: null`, which is the "unknown" branch instead.
 */

function member(over: Partial<MeshMember> = {}): MeshMember {
  return {
    mac: "02:1F:6C:44:9A:31",
    node: "0",
    node_number: 0,
    hop: 0,
    role: "root",
    mesh_type: "Root",
    ip: "192.168.30.121",
    rssi: null,
    rssi_band: null,
    ...over,
  };
}

function probe(over: Partial<MeshProbe> = {}): MeshProbe {
  return {
    probed: true,
    mesh: true,
    members: [member()],
    detail: "",
    captured_at: "2026-08-28 13:17:42",
    ...over,
  };
}

function topology(members: MeshMember[]): MeshTopology {
  return {
    dut: "default",
    mgmt_url: "https://192.168.30.121:10443",
    captured_at: "2026-08-28 13:20:00",
    members,
  };
}

function entry(over: Partial<FleetEntry> = {}): FleetEntry {
  return {
    id: "default",
    label: "Bench AP",
    status: "streaming",
    serialOpen: true,
    lastSerial: null,
    remote: null,
    mgmtUrl: "https://192.168.30.121",
    model: "AP6_840E",
    modelCores: 4,
    deviceId: null,
    meshProbe: null,
    backhaul: {
      applicable: true,
      captured: false,
      consoleId: "console-default",
      role: null,
      uplink: null,
      downlink: null,
    },
    cpuBusyPct: 12,
    coreCount: 4,
    crashCount: 0,
    lastSnapshotTs: null,
    readingDeviceId: null,
    lastEventAgeSec: null,
    ...over,
  };
}

describe("a mesh that something has evidence of", () => {
  it("reads members off the live table", () => {
    expect(meshPresence([entry()], topology([member()]))).toBe("members");
  });

  it("reads them off a stored probe when no table was read", () => {
    // The engineer's path: the table is admin-only, the probe is not.
    expect(meshPresence([entry({ meshProbe: probe() })], null)).toBe("members");
  });

  it("lets one DUT's members outrank another's standalone answer", () => {
    /* Two DUTs can legitimately disagree — one AP standing alone says nothing
       about the rest of the bench, and the page must not collapse around the
       one that answered "no". */
    const fleet = [
      entry({ id: "a", meshProbe: probe({ mesh: false, members: [] }) }),
      entry({ id: "b", meshProbe: probe() }),
    ];
    expect(meshPresence(fleet, null)).toBe("members");
  });

  it("lets the table outrank a probe that answered no", () => {
    const fleet = [entry({ meshProbe: probe({ mesh: false, members: [] }) })];
    expect(meshPresence(fleet, topology([member()]))).toBe("members");
  });
});

describe("a device that answered it has no mesh", () => {
  it("takes an empty table as the answer it is", () => {
    expect(meshPresence([entry()], topology([]))).toBe("none");
  });

  it("takes an empty probe as the answer it is", () => {
    // The 840E on the bench, verbatim: empty list, error_code 0.
    expect(meshPresence([entry({ meshProbe: probe({ mesh: false, members: [] }) })], null)).toBe(
      "none",
    );
  });
});

describe("what nobody has established", () => {
  it("says unknown before anything has asked", () => {
    expect(meshPresence([entry()], null)).toBe("unknown");
  });

  it("does not turn 'could not tell' into 'no mesh'", () => {
    /* The one that matters. A console busy with sysMon returns `mesh: null`,
       and a caller that read that as "this AP stands alone" would hide DUTs off
       the Fleet page because a read failed. */
    const fleet = [entry({ meshProbe: probe({ mesh: null, members: [], detail: "no answer" }) })];
    expect(meshPresence(fleet, null)).toBe("unknown");
  });

  it("does not count a probe that listed nothing while claiming a mesh", () => {
    // Should not occur — the backend reports mesh true only for a non-empty
    // list — but "members" here would be a membership with no member in it.
    const fleet = [entry({ meshProbe: probe({ mesh: true, members: [] }) })];
    expect(meshPresence(fleet, null)).toBe("unknown");
  });

  it("stays unknown on an empty fleet", () => {
    expect(meshPresence([], null)).toBe("unknown");
  });
});
