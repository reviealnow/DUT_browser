import { describe, expect, it } from "vitest";

import type { MeshMember, MeshProbe, MeshTopology } from "../api/rest";
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
