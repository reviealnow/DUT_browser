// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MeshProbe, MeshTopology, Role } from "../api/rest";
import type { RemoteRssiState } from "../monitoring/RemoteRssiContext";
import type { FleetEntry } from "../monitoring/useFleetMonitor";

/**
 * Which DUTs the Fleet page draws, when the bench is not a mesh.
 *
 * A registry entry outlives a lot: DUTs accumulate, and on a bench where the
 * one device on a console reports no mesh at all, a grid of every entry ever
 * registered invites the reader to look for a topology that does not exist.
 * So the grid collapses to the DUT actually on a console.
 *
 * Everything worth testing here is a way that could go wrong quietly:
 *
 *  - collapsing on a FAILED read rather than an answered one, so a busy serial
 *    line makes half the bench disappear;
 *  - collapsing to nothing, leaving an empty grid where the DUTs still are;
 *  - hiding a registered DUT with no count and no way back, which is
 *    indistinguishable from the app having lost it;
 *  - ignoring an admin's live table because the decision was taken outside the
 *    provider that holds it.
 *
 * The last one is why `FleetBody` exists as a separate component, and it is
 * asserted through the rendered page rather than the helper, because that split
 * is exactly the thing a refactor would undo.
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

let fleet: FleetEntry[] = [];
vi.mock("../monitoring/useFleetMonitor", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/useFleetMonitor")>()),
  useFleetMonitor: () => ({ fleet, refreshRegistry: async () => undefined }),
}));

vi.mock("../monitoring/useLastRecommendation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/useLastRecommendation")>()),
  useFleetRecommendations: () => new Map(),
}));

const RSSI_STATE: RemoteRssiState = {
  get: (entry: FleetEntry) => ({
    dut: entry.id,
    applicable: entry.backhaul.applicable,
    captured: entry.backhaul.captured,
    console_id: entry.backhaul.consoleId,
    role: entry.backhaul.role,
    uplink: entry.backhaul.uplink,
    downlink: entry.backhaul.downlink,
  }),
  capturing: () => false,
  refresh: async () => undefined as never,
  refreshAll: async () => undefined,
};
vi.mock("../monitoring/RemoteRssiContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/RemoteRssiContext")>()),
  useRemoteRssi: () => RSSI_STATE,
}));

// A card's connect button drives a serial RPC chain; nothing here is about that.
vi.mock("../monitoring/siteSurveyStore", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/siteSurveyStore")>()),
  runConnectCaptures: async () => undefined,
}));

const { default: FleetSection } = await import("./FleetSection");

function probe(over: Partial<MeshProbe> = {}): MeshProbe {
  return {
    probed: true,
    mesh: true,
    members: [
      {
        mac: "02:1F:6C:44:9A:31",
        node: "0",
        node_number: 0,
        hop: 0,
        role: "root",
        mesh_type: "Root",
        ip: "192.168.30.121",
        rssi: null,
        rssi_band: null,
      },
    ],
    detail: "",
    captured_at: "2026-08-28 13:17:42",
    ...over,
  };
}

/** What the 840E on this bench actually answered on 2026-08-28. */
const NO_MESH: MeshProbe = probe({ mesh: false, members: [], detail: "empty mesh list" });

function entry(id: string, over: Partial<FleetEntry> = {}): FleetEntry {
  return {
    id,
    label: `DUT ${id}`,
    status: "streaming",
    serialOpen: false,
    lastSerial: null,
    remote: null,
    mgmtUrl: "",
    model: "AP6_840E",
    modelCores: 4,
    deviceId: null,
    meshProbe: null,
    backhaul: {
      applicable: true,
      captured: false,
      consoleId: `console-${id}`,
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

function show() {
  render(<FleetSection onSelectDut={() => undefined} onOpenConsole={() => undefined} />);
}

/** The labels of the cards actually drawn, in order. */
function cardLabels(): string[] {
  return Array.from(document.querySelectorAll(".fleet-card .card-title")).map(
    (node) => node.textContent ?? "",
  );
}

beforeEach(() => {
  role = "admin";
  getMeshTopology.mockReset();
  // Default: the table read never resolves, so unless a test says otherwise the
  // decision rests on the probes — the engineer's view of this page.
  getMeshTopology.mockReturnValue(new Promise(() => {}));
});

afterEach(cleanup);

describe("a bench whose DUT answered that it is in no mesh", () => {
  it("shows only the DUT on a console, and says what it hid", () => {
    fleet = [
      entry("a", { serialOpen: true, meshProbe: NO_MESH }),
      entry("b"),
      entry("c"),
    ];
    show();
    expect(cardLabels()).toEqual(["DUT a"]);
    expect(screen.getByText(/hides 2 others/)).toBeTruthy();
  });

  it("still counts every registered DUT in the line above", () => {
    // The count is the reader's cross-check. "1 registered" over one card while
    // three exist would be the app lying rather than filtering.
    fleet = [entry("a", { serialOpen: true, meshProbe: NO_MESH }), entry("b"), entry("c")];
    show();
    expect(screen.getByText(/3 registered · 1 with a console open/)).toBeTruthy();
  });

  it("gives the hidden DUTs back on request", () => {
    fleet = [entry("a", { serialOpen: true, meshProbe: NO_MESH }), entry("b"), entry("c")];
    show();
    fireEvent.click(screen.getByText("Show all"));
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
  });

  it("shows both DUTs when two consoles are open", () => {
    // "The console" is not necessarily one. Filtering to the first would drop a
    // DUT somebody is actively working on.
    fleet = [
      entry("a", { serialOpen: true, meshProbe: NO_MESH }),
      entry("b", { serialOpen: true }),
      entry("c"),
    ];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b"]);
  });
});

describe("when the page must not collapse", () => {
  it("leaves every DUT visible when nobody has asked about a mesh", () => {
    /* `unknown` is not `none`. Before any probe runs there is no evidence of
       anything, and hiding DUTs on no evidence is not a filter, it is a fault. */
    fleet = [entry("a", { serialOpen: true }), entry("b"), entry("c")];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
    expect(screen.queryByText("Show all")).toBeNull();
  });

  it("leaves every DUT visible when the probe could not tell", () => {
    /* The failure this guard exists for: sysMon saturates the console and the
       probe returns `mesh: null`. Read as "no mesh", a busy serial line would
       make most of the bench disappear off the page. */
    fleet = [
      entry("a", { serialOpen: true, meshProbe: probe({ mesh: null, members: [] }) }),
      entry("b"),
      entry("c"),
    ];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
  });

  it("leaves every DUT visible when a mesh has members", () => {
    fleet = [entry("a", { serialOpen: true, meshProbe: probe() }), entry("b"), entry("c")];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
  });

  it("never empties the grid when no console is open", () => {
    /* Collapsing to nothing removes the noise and the data with it. The DUTs
       are still registered and the page is still the only place to see them. */
    fleet = [entry("a", { meshProbe: NO_MESH }), entry("b"), entry("c")];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
  });

  it("says nothing about hiding when there is nothing to hide", () => {
    // One DUT, no mesh, console open: already the whole fleet.
    fleet = [entry("a", { serialOpen: true, meshProbe: NO_MESH })];
    show();
    expect(cardLabels()).toEqual(["DUT a"]);
    expect(screen.queryByText(/hides/)).toBeNull();
  });
});

describe("the admin's live table, which only the provider holds", () => {
  /* The decision cannot be taken in the component that MOUNTS the provider —
     it cannot consume it — so it lives one level down. These two tests are what
     notices if that split is ever flattened back. */

  it("does not collapse when the table lists members", async () => {
    const topology: MeshTopology = {
      dut: "a",
      mgmt_url: "https://192.168.30.121:10443",
      captured_at: "2026-08-28 13:20:00",
      members: probe().members,
    };
    getMeshTopology.mockResolvedValue(topology);
    fleet = [
      entry("a", { serialOpen: true, mgmtUrl: "https://192.168.30.121" }),
      entry("b", { mgmtUrl: "https://192.168.30.176" }),
    ];
    show();
    await waitFor(() => expect(getMeshTopology).toHaveBeenCalled());
    await waitFor(() => expect(cardLabels()).toEqual(["DUT a", "DUT b"]));
  });

  it("collapses when the table answers with an empty list", async () => {
    getMeshTopology.mockResolvedValue({
      dut: "a",
      mgmt_url: "https://192.168.30.121:10443",
      captured_at: "2026-08-28 13:20:00",
      members: [],
    });
    fleet = [
      entry("a", { serialOpen: true, mgmtUrl: "https://192.168.30.121" }),
      entry("b", { mgmtUrl: "https://192.168.30.176" }),
    ];
    show();
    await waitFor(() => expect(cardLabels()).toEqual(["DUT a"]));
  });
});
