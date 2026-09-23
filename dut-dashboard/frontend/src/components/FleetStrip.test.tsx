// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Role } from "../api/rest";
import type { RemoteRssiState } from "../monitoring/RemoteRssiContext";
import type { FleetEntry } from "../monitoring/useFleetMonitor";

/**
 * Which DUTs the Overview strip draws.
 *
 * Nothing in this app expires a registration: a Pi console attached once is
 * persisted and only an explicit Remove takes it out, so the strip used to
 * carry every DUT ever registered, dead ones included, on the page meant for a
 * glance. It now collapses to the DUTs on a console.
 *
 * Everything worth testing here is a way that could go wrong quietly, and they
 * are the Fleet page's failure modes because this is the Fleet page's rule:
 *
 *  - collapsing to nothing, leaving an empty strip where the DUTs still are;
 *  - hiding a registered DUT with no count and no way back, which is
 *    indistinguishable from the app having lost it -- and here the hidden card
 *    holds that DUT's only Connect button;
 *  - a count that stops matching the cards under it.
 */

let role: Role = "admin";
vi.mock("../monitoring/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/AuthContext")>()),
  useAuth: () => ({ role }),
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

const { default: FleetStrip } = await import("./FleetStrip");

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

/** A console held on a Pi — driving one of these needs admin, not engineer. */
const REMOTE = { host: "192.168.30.145", port: 22, device: "/dev/ttyUSB0" };

function show() {
  render(<FleetStrip onSelectDut={() => undefined} onOpenConsole={() => undefined} />);
}

/** The labels of the cards actually drawn, in order. */
function cardLabels(): string[] {
  return Array.from(document.querySelectorAll(".fleet-card .card-title")).map(
    (node) => node.textContent ?? "",
  );
}

beforeEach(() => {
  role = "admin";
});

afterEach(cleanup);

describe("a bench carrying registrations nobody has a console on", () => {
  it("shows only the DUTs on a console, and says what it hid", () => {
    fleet = [entry("a", { serialOpen: true }), entry("b"), entry("c")];
    show();
    expect(cardLabels()).toEqual(["DUT a"]);
    expect(screen.getByText(/hides 2 others/)).toBeTruthy();
  });

  it("counts every registered DUT in the line above", () => {
    // The count is the reader's cross-check. "1 registered" over one card while
    // three exist would be the app lying rather than filtering.
    fleet = [entry("a", { serialOpen: true }), entry("b"), entry("c")];
    show();
    expect(screen.getByText(/3 registered · 1 with a console open/)).toBeTruthy();
  });

  it("says the hidden cards take their Connect buttons with them", () => {
    // The card is the only place a remembered DUT can be reconnected from, so
    // hiding it hides the way back. Saying so is the difference between a
    // filter and a disappearance.
    fleet = [entry("a", { serialOpen: true }), entry("b")];
    show();
    expect(screen.getByText(/hides 1 other — with its Connect button/)).toBeTruthy();
  });

  it("gives the hidden DUTs back on request, and takes them away again", () => {
    fleet = [entry("a", { serialOpen: true }), entry("b"), entry("c")];
    show();
    fireEvent.click(screen.getByText("Show all"));
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
    fireEvent.click(screen.getByText("Show only open consoles"));
    expect(cardLabels()).toEqual(["DUT a"]);
  });

  it("keeps every open console, not just the first", () => {
    fleet = [
      entry("a", { serialOpen: true }),
      entry("b", { serialOpen: true }),
      entry("c"),
    ];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b"]);
  });
});

describe("when the strip must not collapse", () => {
  it("shows every DUT when no console is open at all", () => {
    // Collapsing to an empty strip would take the whole bench off the page --
    // including every Connect button that could put a console back.
    fleet = [entry("a"), entry("b"), entry("c")];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b", "DUT c"]);
  });

  it("says nothing when every DUT has a console open", () => {
    // Nothing is hidden, so the toolbar would be a line of text announcing that
    // it had done nothing.
    fleet = [entry("a", { serialOpen: true }), entry("b", { serialOpen: true })];
    show();
    expect(cardLabels()).toEqual(["DUT a", "DUT b"]);
    expect(screen.queryByText(/registered/)).toBeNull();
  });

  it("does not promise a Connect button to a reader who has none", () => {
    // Driving a remote node needs admin. An engineer sees no Connect button on
    // these cards at all, so naming one would send them looking for something
    // that was never on the page -- the sentence has to describe this reader's
    // screen, not the most privileged one.
    role = "engineer";
    fleet = [entry("a", { serialOpen: true }), entry("b", { remote: REMOTE })];
    show();
    expect(cardLabels()).toEqual(["DUT a"]);
    expect(screen.getByText(/hides 1 other\./)).toBeTruthy();
    expect(screen.queryByText(/Connect button/)).toBeNull();
  });

  it("stays hidden entirely for a single DUT, open or not", () => {
    fleet = [entry("a", { serialOpen: true })];
    show();
    expect(cardLabels()).toEqual([]);
    expect(screen.queryByText(/registered/)).toBeNull();
  });
});
