// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RemoteRssiResult, RemoteUplink, Role } from "../api/rest";
import type { RemoteRssiState } from "../monitoring/RemoteRssiContext";
import type { FleetEntry } from "../monitoring/useFleetMonitor";

/**
 * What a fleet card tells a human, and which controls it offers whom.
 *
 * The four things the two backhaul rows can say are four different claims about
 * a DUT, and telling them apart is the whole point of the capture: "nobody has
 * measured this" is not "this is the root", and neither is "measured, and
 * nothing here belongs to a mesh". Getting one wrong prints a confident wrong
 * answer on somebody's screen, which is the failure mode this area keeps
 * producing and the one no backend test can see.
 *
 * The role gates are here for the same reason. Every `/api/fleet` route is
 * admin, so a Refresh RSSI drawn for an engineer is a button that can only
 * answer 403 — and it was drawn for them once, because the capture borrowed a
 * gate meant for the serial routes.
 *
 * The role comes from a stubbed `useAuth` and nothing else: `ROLE_RANK` stays
 * real, so what is under test is this component's gating rather than how a
 * session is fetched.
 */

let role: Role = "admin";
vi.mock("../monitoring/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/AuthContext")>()),
  useAuth: () => ({ role }),
}));

const { default: FleetCard } = await import("./FleetCard");

const UPLINK: RemoteUplink = {
  iface: "ath15",
  rssi: -37,
  snr: 55,
  rssi_band: "near",
  radio_band: "5GHz",
  essid: "backhaul",
  peer_mac: "ce:4f:86:95:ce:e5",
};

function entry(overrides: Partial<FleetEntry> = {}): FleetEntry {
  return {
    id: "default",
    label: "Bench AP",
    status: "streaming",
    serialOpen: true,
    lastSerial: { port: "/dev/cu.bench", baudrate: 115200 },
    remote: null,
    mgmtUrl: "",
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
    ...overrides,
  };
}

/** A card, rendered with the reading the registry would have handed it. */
function show(fleetEntry: FleetEntry, reading?: Partial<RemoteRssiResult>) {
  const rssi: RemoteRssiResult = {
    dut: fleetEntry.id,
    applicable: fleetEntry.backhaul.applicable,
    captured: fleetEntry.backhaul.captured,
    console_id: fleetEntry.backhaul.consoleId,
    role: fleetEntry.backhaul.role,
    uplink: fleetEntry.backhaul.uplink,
    downlink: fleetEntry.backhaul.downlink,
    ...reading,
  };
  const rssiState: RemoteRssiState = {
    get: () => rssi,
    capturing: () => false,
    refresh: async () => rssi,
    refreshAll: async () => undefined,
  };
  render(
    <FleetCard
      entry={fleetEntry}
      reco={undefined}
      rssiState={rssiState}
      variant="grid"
      onOpen={() => undefined}
      onConsole={() => undefined}
      onClosed={async () => undefined}
    />,
  );
}

/** The `<dd>` beside a row's label.
 *
 *  Anchored on the `<dt>`: in the grid variant the unfolded capture below the
 *  card repeats both labels as headings, so an unqualified lookup matches two
 *  elements and the compressed row — the one the strip shows — is not
 *  necessarily the first. */
function row(label: string): string {
  const term = screen.getByText(label, { selector: "dt" });
  const value = term.parentElement?.querySelector("dd");
  return value?.textContent?.trim() ?? "";
}

afterEach(() => {
  role = "admin";
  cleanup();
});

/** A probe result, defaulting to the healthy meshed case. */
function probe(over: Partial<import("../api/rest").MeshProbe> = {}) {
  return {
    probed: true,
    mesh: true as boolean | null,
    members: [{ mac: "02:1f:6c:44:9a:31" }] as never,
    detail: "",
    captured_at: "2026-08-24 11:02:33",
    ...over,
  };
}

describe("what the device itself says about its mesh", () => {
  /* Four claims, and the last two are the ones that matter. "We asked and could
     not tell" is not "this DUT has no mesh" — printing the second when the first
     is true puts a confident wrong answer on somebody's screen about a device
     that is meshed and healthy, which is the failure this whole feature exists
     to avoid. */
  it("says nobody has asked yet", () => {
    show(entry());
    expect(row("Mesh (device says)")).toBe("Not probed");
  });

  it("reports the members a meshed device listed", () => {
    show(entry({ meshProbe: probe() }));
    expect(row("Mesh (device says)")).toBe("1 member reported");
  });

  it("says no mesh only when the device answered with an empty list", () => {
    show(entry({ meshProbe: probe({ mesh: false, members: [], detail: "empty" }) }));
    expect(row("Mesh (device says)")).toBe("No mesh on this device");
  });

  it("keeps 'could not tell' distinct from 'no mesh'", () => {
    show(entry({ meshProbe: probe({ mesh: null, members: [], detail: "mesh not enabled" }) }));
    expect(row("Mesh (device says)")).toBe("Could not tell");
  });

  it("does not overwrite the admin's standalone declaration", () => {
    /* A DUT declared standalone that reports mesh members is the case worth
       seeing, so both statements stay on the card. Folding the measurement into
       `applicable` would delete the only signal that they disagree. */
    show(entry({
      backhaul: { ...entry().backhaul, applicable: false },
      meshProbe: probe(),
    }));
    expect(row("Uplink to parent")).toBe("Not applicable");
    expect(row("Mesh (device says)")).toBe("1 member reported");
  });
});

describe("the four things the backhaul rows can say", () => {
  it("says nobody has measured this DUT yet", () => {
    show(entry());
    expect(row("Uplink to parent")).toBe("Not captured");
    expect(row("Children on backhaul")).toBe("Not captured");
  });

  it("says a measured DUT is the root, rather than reporting a gap", () => {
    show(entry({
      backhaul: { ...entry().backhaul, captured: true, role: "root" },
    }));
    expect(row("Uplink to parent")).toBe("None — this is the root");
  });

  it("does not call a cabled DUT with no parent a root", () => {
    // A standalone AP has no parent either. Claiming the root of a mesh for it
    // is a wrong answer on the commonest desk setup there is.
    show(entry({
      backhaul: { ...entry().backhaul, captured: true, role: null },
    }));
    expect(row("Uplink to parent")).toBe("None — no parent found");
    expect(row("Children on backhaul")).toBe("No backhaul VAP identified");
  });

  it("says not applicable only where an admin declared a node standalone", () => {
    show(entry({
      remote: { host: "10.0.0.24", port: 22, device: "/dev/ttyUSB0" },
      backhaul: { ...entry().backhaul, applicable: false },
    }));
    expect(row("Uplink to parent")).toBe("Not applicable");
    expect(row("Children on backhaul")).toBe("Not applicable");
  });

  it("shows the measurement when there is one", () => {
    show(entry({
      backhaul: { ...entry().backhaul, captured: true, role: "node", uplink: UPLINK },
    }));
    expect(row("Uplink to parent")).toBe("-37 dBm · near");
  });

  it("names an interface nobody verified is a backhaul, with the SSID it serves", () => {
    // On this bench a configured VAP was carrying an ordinary laptop, and the
    // card reported that laptop as a mesh child with no provenance.
    show(entry({
      backhaul: {
        ...entry().backhaul,
        captured: true,
        role: "root",
        downlink: {
          iface: "ath32",
          source: "configured",
          essid: "!!3290-1",
          peers: [{ mac: "f4:3b:d8:d6:98:8b", rssi: -42, rssi_band: "near" }],
        },
      },
    }));
    expect(row("Children on backhaul")).toBe("-42 dBm · ath32 configured (!!3290-1)");
  });
});

describe("which rows belong to which kind of DUT", () => {
  it("gives a cabled DUT the backhaul rows and no SSH rows", () => {
    show(entry());
    expect(screen.getByText("Mother server")).toBeTruthy();
    expect(screen.getByText("Uplink to parent")).toBeTruthy();
    expect(screen.queryByText("SSH session")).toBeNull();
  });

  it("gives a remote node both", () => {
    show(entry({ remote: { host: "10.0.0.24", port: 22, device: "/dev/ttyUSB0" } }));
    expect(screen.getByText("SSH session")).toBeTruthy();
    expect(screen.getByText("Uplink to parent")).toBeTruthy();
  });
});

describe("who is offered the capture", () => {
  const refreshButton = () =>
    screen.queryAllByRole("button").find((button) => button.textContent === "Refresh RSSI");

  it("offers it to an admin", () => {
    role = "admin";
    show(entry());
    expect(refreshButton()).toBeTruthy();
  });

  it("does not draw it for an engineer", () => {
    // Every /api/fleet route is admin. A button that can only answer 403 is
    // worse than no button — and this one is not `canDrive`, which is engineer
    // for a cabled DUT.
    role = "engineer";
    show(entry());
    expect(refreshButton()).toBeUndefined();
  });

  it("does not draw it for a guest", () => {
    role = "guest";
    show(entry());
    expect(refreshButton()).toBeUndefined();
  });

  it("disables it when the console is closed", () => {
    show(entry({ serialOpen: false }));
    expect(refreshButton()?.hasAttribute("disabled")).toBe(true);
  });

  it("disables it, with the reason, for a node declared standalone", () => {
    show(entry({
      remote: { host: "10.0.0.24", port: 22, device: "/dev/ttyUSB0" },
      backhaul: { ...entry().backhaul, applicable: false },
    }));
    const button = refreshButton();
    expect(button?.hasAttribute("disabled")).toBe(true);
    expect(button?.getAttribute("title")).toContain("Standalone AP");
  });
});

describe("the unfolded capture, in the Fleet section", () => {
  it("tells 'no capture yet' from 'captured, and there is no backhaul here'", () => {
    show(entry());
    expect(screen.getByText(/No capture yet/)).toBeTruthy();

    cleanup();
    show(entry({ backhaul: { ...entry().backhaul, captured: true, role: null } }));
    const detail = screen.getByText(/Captured: no parent/);
    expect(within(detail).getByText(/standalone/)).toBeTruthy();
  });
});
