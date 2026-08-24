// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MeshTopology, Role } from "../api/rest";
import type { FleetEntry } from "../monitoring/useFleetMonitor";

/**
 * What the mesh table claims, and what it must never claim.
 *
 * This view exists because the Fleet and the DUT's console disagreed: the
 * device listed a root and a node, the UI showed one card. So the two failures
 * worth testing are the ones that would recreate that, quietly:
 *
 *  - a member the dashboard has no console for must still be listed, and
 *    visibly marked as unregistered rather than blending in;
 *  - a root's absent signal must not print as a number. The device sends 0 for
 *    it, and `0 dBm` on a fleet screen reads as the best link on the bench.
 *
 * Matching is asserted through the rendered row rather than the helper alone,
 * because the bug would be in what a person reads.
 */

let role: Role = "admin";
vi.mock("../monitoring/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/AuthContext")>()),
  useAuth: () => ({ role }),
}));

const getMeshTopology = vi.fn();
vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getMeshTopology: (dutId: string) => getMeshTopology(dutId),
}));

const { default: MeshTopologySection, hostOf, matchMember, meshSources } = await import(
  "./MeshTopology"
);

/** The real reply from AP6_420E, in the shape the backend publishes it. */
const TOPOLOGY: MeshTopology = {
  dut: "default",
  mgmt_url: "https://192.168.30.121:10443",
  captured_at: "2026-08-24T11:02:33",
  members: [
    {
      mac: "C8:4F:86:91:47:E1",
      node: "0",
      node_number: 0,
      hop: 0,
      role: "root",
      mesh_type: "Root",
      ip: "192.168.30.121",
      rssi: null,
      rssi_band: null,
    },
    {
      mac: "C8:4F:86:89:F1:68",
      node: "1",
      node_number: 1,
      hop: 1,
      role: "node",
      mesh_type: "Node",
      ip: "192.168.30.176",
      rssi: -26,
      rssi_band: "near",
    },
  ],
};

function entry(over: Partial<FleetEntry> & { id: string }): FleetEntry {
  return {
    label: over.id,
    status: "idle",
    serialOpen: false,
    lastSerial: null,
    remote: null,
    mgmtUrl: "",
    meshProbe: null,
    backhaul: {
      applicable: true,
      captured: false,
      consoleId: "c1",
      role: null,
      uplink: null,
      downlink: null,
    },
    cpuBusyPct: null,
    coreCount: 0,
    crashCount: 0,
    lastSnapshotTs: null,
    lastEventAgeSec: null,
    ...over,
  };
}

/** One registered DUT, the root, with a console open. The node is absent —
 *  which is exactly the fleet that produced the original report. */
const FLEET: FleetEntry[] = [
  entry({
    id: "default",
    label: "AP6_420E",
    serialOpen: true,
    mgmtUrl: "https://192.168.30.121:10443",
  }),
];

async function rows(): Promise<HTMLElement[]> {
  const table = await screen.findByRole("table");
  return within(table).getAllByRole("row").slice(1); // drop the header
}

afterEach(cleanup);
beforeEach(() => {
  role = "admin";
  getMeshTopology.mockReset();
  getMeshTopology.mockResolvedValue(TOPOLOGY);
});

describe("mesh topology table", () => {
  it("lists a mesh member this dashboard has no console for", async () => {
    render(<MeshTopologySection fleet={FLEET} />);
    const listed = await rows();
    expect(listed).toHaveLength(2);
    expect(within(listed[1]).getByText("192.168.30.176")).toBeTruthy();
    expect(within(listed[1]).getByText("C8:4F:86:89:F1:68")).toBeTruthy();
  });

  it("says which members are unknown to it rather than letting them blend in", async () => {
    render(<MeshTopologySection fleet={FLEET} />);
    const listed = await rows();
    expect(within(listed[0]).getByText(/AP6_420E/)).toBeTruthy();
    expect(within(listed[0]).getByText(/console open/)).toBeTruthy();
    expect(within(listed[1]).getByText("Not registered here")).toBeTruthy();
  });

  it("never prints a root's missing signal as a number", async () => {
    render(<MeshTopologySection fleet={FLEET} />);
    const listed = await rows();
    expect(within(listed[0]).getByText(/n\/a — root/)).toBeTruthy();
    expect(within(listed[0]).queryByText(/dBm/)).toBeNull();
    expect(within(listed[1]).getByText("-26 dBm · near")).toBeTruthy();
  });

  it("asks the DUT once on entry, without waiting for a click", async () => {
    render(<MeshTopologySection fleet={FLEET} />);
    await waitFor(() => expect(getMeshTopology).toHaveBeenCalledWith("default"));
    expect(getMeshTopology).toHaveBeenCalledTimes(1);
  });

  it("shows an error instead of a stale table when the read fails", async () => {
    getMeshTopology.mockRejectedValue(new Error("The DUT rejected the credentials (401)."));
    render(<MeshTopologySection fleet={FLEET} />);
    expect(await screen.findByText(/rejected the credentials/)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("tells an empty mesh apart from a failed read", async () => {
    getMeshTopology.mockResolvedValue({ ...TOPOLOGY, members: [] });
    render(<MeshTopologySection fleet={FLEET} />);
    expect(await screen.findByText(/reports no mesh members/)).toBeTruthy();
  });

  it("offers an engineer an explanation rather than a control that can only 403", async () => {
    role = "engineer";
    render(<MeshTopologySection fleet={FLEET} />);
    expect(screen.getByText(/admin-only/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(getMeshTopology).not.toHaveBeenCalled();
  });

  it("says so when no DUT has a management address to ask", () => {
    role = "admin";
    render(<MeshTopologySection fleet={[entry({ id: "lab2" })]} />);
    expect(screen.getByText(/No DUT has a management address/)).toBeTruthy();
    expect(getMeshTopology).not.toHaveBeenCalled();
  });

  it("offers a picker only when more than one DUT can be asked", async () => {
    const { unmount } = render(<MeshTopologySection fleet={FLEET} />);
    await waitFor(() => expect(getMeshTopology).toHaveBeenCalled());
    expect(screen.queryByLabelText(/DUT to ask/)).toBeNull();
    unmount();

    render(
      <MeshTopologySection
        fleet={[...FLEET, entry({ id: "lab2", mgmtUrl: "https://192.168.30.176" })]}
      />,
    );
    expect(await screen.findByLabelText(/DUT to ask/)).toBeTruthy();
  });
});

describe("matching a member to a registered DUT", () => {
  it("matches on the management address, not on the Pi holding the console", () => {
    const node = TOPOLOGY.members[1];
    // `remote.host` is the console server, a different machine on a different
    // address. Matching on it would attach a mesh member to the wrong device.
    const viaPi = entry({
      id: "node1",
      remote: { host: "192.168.30.176", port: 22, device: "/dev/ttyUSB0" },
    });
    expect(matchMember(node, [viaPi])).toBeNull();

    const viaMgmt = entry({ id: "node1", mgmtUrl: "https://192.168.30.176:10443" });
    expect(matchMember(node, [viaMgmt])?.id).toBe("node1");
  });

  it("matches nothing when the member has no address", () => {
    expect(matchMember({ ...TOPOLOGY.members[1], ip: null }, FLEET)).toBeNull();
  });

  it("treats an unusable management address as no address at all", () => {
    expect(hostOf("")).toBeNull();
    expect(hostOf("   ")).toBeNull();
    expect(hostOf("https://192.168.30.121:10443")).toBe("192.168.30.121");
    expect(hostOf("192.168.30.121")).toBe("192.168.30.121");
    expect(meshSources([entry({ id: "a" }), entry({ id: "b", mgmtUrl: "10.0.0.5" })])).toHaveLength(
      1,
    );
  });
});
