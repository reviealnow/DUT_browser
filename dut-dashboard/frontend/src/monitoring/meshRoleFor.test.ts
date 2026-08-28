import { describe, expect, it } from "vitest";

import type { MeshMember, MeshProbe, MeshTopology } from "../api/rest";
import {
  BENCH_MESH_MEMBERS,
  BENCH_MESH_PROBE,
  BENCH_NODE_MGMT,
  BENCH_ROOT_MGMT,
} from "./benchMesh.fixture";
import { meshRoleFor } from "./MeshTopologyContext";
import type { FleetEntry } from "./useFleetMonitor";

/**
 * Which reading a card's mesh role comes from, and when it must come from none.
 *
 * Two sources answer the same question and they are not interchangeable. The
 * mesh table is read from a device now and only an admin on the Fleet page has
 * one; the probe is what a DUT last said about itself over its own console, and
 * it arrives on every fleet entry for free. The fallback exists so Overview can
 * name a role without that page ever addressing a DUT — so the test that
 * matters most here is that no request is implied: these are pure calls.
 *
 * Null is the third answer and the easiest to lose. A DUT neither source names
 * is not a leaf, and "we asked and could not tell" is not "no mesh".
 */

const HOST = "192.168.30.121";

function member(over: Partial<MeshMember> = {}): MeshMember {
  return {
    mac: "02:1F:6C:44:9A:31",
    node: "0",
    node_number: 0,
    hop: 0,
    role: "root",
    mesh_type: "Root",
    ip: HOST,
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
    captured_at: "2026-08-27 11:02:33",
    ...over,
  };
}

function topology(members: MeshMember[]): MeshTopology {
  return {
    dut: "default",
    mgmt_url: `https://${HOST}`,
    captured_at: "2026-08-28 09:00:00",
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
    mgmtUrl: `https://${HOST}`,
    model: "AP6_420E",
    modelCores: 2,
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
    coreCount: 2,
    crashCount: 0,
    lastSnapshotTs: null,
    lastEventAgeSec: null,
    ...over,
  };
}

describe("where a DUT's mesh role comes from", () => {
  it("prefers the live table, and says that is where it came from", () => {
    const answer = meshRoleFor(entry({ meshProbe: probe() }), topology([member({ role: "node" })]));
    expect(answer).toEqual({ role: "node", source: "mesh table", capturedAt: null });
  });

  it("falls back to the DUT's own probe when no table was read", () => {
    // Overview mounts no provider, so this is the path every card there takes.
    const answer = meshRoleFor(entry({ meshProbe: probe() }), null);
    expect(answer).toEqual({
      role: "root",
      source: "device probe",
      capturedAt: "2026-08-27 11:02:33",
    });
  });

  it("falls back when a table was read but does not list this DUT", () => {
    /* A table is one device's account of the mesh and can be missing a member
       this DUT knows about itself. Reading "no answer" off it while the DUT's
       own probe holds one would throw away the better evidence. */
    const answer = meshRoleFor(
      entry({ meshProbe: probe() }),
      topology([member({ ip: "192.168.30.176", role: "node" })]),
    );
    expect(answer?.source).toBe("device probe");
    expect(answer?.role).toBe("root");
  });

  it("carries the probe's own timestamp, not the table's", () => {
    // The card prints this on hover. A role dated "now" that was measured last
    // week is the whole thing the source field exists to prevent.
    expect(meshRoleFor(entry({ meshProbe: probe() }), null)?.capturedAt).toBe(
      "2026-08-27 11:02:33",
    );
  });
});

describe("when a mesh role must stay unanswered", () => {
  it("says nothing when neither source exists", () => {
    expect(meshRoleFor(entry(), null)).toBeNull();
  });

  it("does not read a role out of 'we could not tell'", () => {
    /* `mesh: null` is a failed question, not a device without a mesh. A member
       list that came back empty for that reason must not become a membership. */
    const answer = meshRoleFor(
      entry({ meshProbe: probe({ mesh: null, members: [member()], detail: "no answer" }) }),
      null,
    );
    expect(answer).toBeNull();
  });

  it("does not read a role out of a device that reported no mesh", () => {
    expect(meshRoleFor(entry({ meshProbe: probe({ mesh: false, members: [] }) }), null)).toBeNull();
  });

  it("does not call a DUT the probe never names a node", () => {
    // The probe lists the mesh, not only this device. A DUT absent from it is
    // absent, and "node" is a membership nothing here measured.
    const answer = meshRoleFor(
      entry({ meshProbe: probe({ members: [member({ ip: "192.168.30.176" })] }) }),
      null,
    );
    expect(answer).toBeNull();
  });

  it("stays silent for a DUT with no management address", () => {
    /* The address is the only thing tying a fleet entry to a row in either
       member list. Without one there is nothing to match on, and matching on
       `remote.host` instead would pin a mesh role to the Pi holding a console
       rather than to the device. */
    const answer = meshRoleFor(entry({ mgmtUrl: "", meshProbe: probe() }), null);
    expect(answer).toBeNull();
  });

  it("ignores a member row whose mesh_type this repo does not recognise", () => {
    const answer = meshRoleFor(
      entry({ meshProbe: probe({ members: [member({ role: null, mesh_type: "Relay" })] }) }),
      null,
    );
    expect(answer).toBeNull();
  });
});


describe("the bench mesh, as the hardware actually answered", () => {
  /* Everything above is constructed, and a constructed fixture agrees with
     whatever its author believed. This one is a two-device mesh captured over a
     real console on 2026-08-28 -- see benchMesh.fixture.ts for the raw bytes and
     for the two oddities in them.

     What it is really guarding is the fallback's reach: on the day this was
     captured the root's console was sitting at a login prompt and could not be
     asked anything, and the node's stored probe still gave the root its role.
     A mesh table could not have covered that, because reading one is admin-only
     and needs a management address the root was never asked for. */

  const root = entry({ id: "root1", mgmtUrl: BENCH_ROOT_MGMT, meshProbe: BENCH_MESH_PROBE });
  const node = entry({ id: "node1", mgmtUrl: BENCH_NODE_MGMT, meshProbe: BENCH_MESH_PROBE });

  it("names the root from a probe taken on the other device", () => {
    expect(meshRoleFor(root, null)).toEqual({
      role: "root",
      source: "device probe",
      capturedAt: BENCH_MESH_PROBE.captured_at,
    });
  });

  it("names the node from the same probe", () => {
    expect(meshRoleFor(node, null)?.role).toBe("node");
  });

  it("resolves both DUTs from one reading, not one each", () => {
    // Two cards, one stored probe, no request. That is the whole shape of it.
    expect([meshRoleFor(root, null)?.role, meshRoleFor(node, null)?.role]).toEqual([
      "root",
      "node",
    ]);
  });

  it("still prefers a live table over the same answer stored", () => {
    const live: MeshTopology = {
      dut: "node1",
      mgmt_url: BENCH_NODE_MGMT,
      captured_at: "2026-08-28 14:00:00",
      members: BENCH_MESH_MEMBERS,
    };
    expect(meshRoleFor(root, live)?.source).toBe("mesh table");
  });

  it("keeps the device's two labels for a member even when they disagree", () => {
    /* `node: "0"` with `node_number: 1`, from the device's own output. The two
       are published separately precisely so this is visible; deriving either
       from the other would have hidden it, and a later tidy-up that
       "normalises" them would be deleting a real observation. */
    const node0 = BENCH_MESH_MEMBERS.find((m) => m.node === "0");
    expect(node0?.node_number).toBe(1);
  });

  it("does not read the root's inapplicable signal as a link", () => {
    /* The device sends `signal: 0` for a root -- no parent to hear -- and the
       backend nulls it. Nothing in the role path should resurrect it. */
    const rootMember = BENCH_MESH_MEMBERS.find((m) => m.role === "root");
    expect(rootMember?.rssi).toBeNull();
    expect(rootMember?.rssi_band).toBeNull();
  });
});
