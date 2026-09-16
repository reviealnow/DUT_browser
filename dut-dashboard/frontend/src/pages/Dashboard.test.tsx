// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DutInfo } from "../api/rest";

/**
 * What the Connection card claims about a session it did not open itself.
 *
 * `isOpen` used to be local state that only this page could set, so a console
 * opened anywhere else — Attach on Fleet > Hosts, another tab, or this page
 * before a reload — left the card saying "Step 1 — select a serial port" over a
 * DUT that was streaming, and every Send came back "Not connected". Reported
 * from the bench with a Raspberry Pi console attached and running.
 *
 * The second half is the port picker itself: a DUT whose console is a `socat`
 * on a box reached over SSH has no local port, and the picker would open a
 * different transport for the same DUT on a cable that is not there.
 */

const getDuts = vi.fn();
vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getDuts: () => getDuts(),
  listSerialPorts: async () => [],
}));

vi.mock("../monitoring/DutMonitorContext", () => ({
  useDutMonitorContext: () => ({ lines: [], linesStartSeq: 0, serialDisconnect: null }),
}));

vi.mock("../monitoring/useCrashKeywords", () => ({
  useCrashKeywords: () => ({
    keywords: [], pattern: null, saving: false, saveKeywords: async () => {},
  }),
}));

const { default: Dashboard } = await import("./Dashboard");

function dut(over: Partial<DutInfo> = {}): DutInfo {
  return {
    id: "pi-ttyusb0",
    label: "192.168.30.124 ttyUSB0",
    mode: "ssh",
    serial_open: true,
    log_path: "/logs/dut-session-pi-20260916-082920.log",
    removable: true,
    mgmt_url: "",
    last_serial: null,
    model: null,
    vaps_per_band: 0,
    bands: [],
    remote: { host: "192.168.30.124", port: 22, device: "/dev/ttyUSB0", is_mesh: false },
    backhaul: null,
    ...over,
  } as DutInfo;
}

async function show(entry: DutInfo) {
  getDuts.mockResolvedValue([entry]);
  render(<Dashboard active dutId={entry.id} />);
  await waitFor(() => expect(getDuts).toHaveBeenCalled());
}

const cardSub = () => document.querySelector(".conn-card .card-sub")?.textContent ?? "";
const openButton = () =>
  [...document.querySelectorAll("button")].find((b) => b.textContent === "Open");

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

describe("a console this page did not open", () => {
  it("is reported as connected, not as step one", async () => {
    await show(dut({ serial_open: true }));
    await waitFor(() => expect(cardSub()).toMatch(/Connected/));
    expect(cardSub()).not.toMatch(/select a serial port/);
  });

  it("can download the log that session is already writing", async () => {
    // The file name is not on screen; what it gates is. The page could only
    // ever know the name of a log it had started itself, so Download DUT Log
    // sat disabled over a running capture.
    await show(dut({ serial_open: true }));
    const download = () =>
      [...document.querySelectorAll("button")].find((b) => b.textContent === "Download DUT Log");
    await waitFor(() => expect(download()?.hasAttribute("disabled")).toBe(false));
  });

  it("still says step one for a cabled DUT with nothing open", async () => {
    await show(dut({ remote: null, serial_open: false, log_path: null, mode: null }));
    await waitFor(() => expect(cardSub()).toMatch(/select a serial port/));
    expect(openButton()).toBeTruthy();
  });
});

describe("a DUT whose console is somebody else's serial port", () => {
  it("offers no local port to open, and says where the console is", async () => {
    await show(dut({ serial_open: false, log_path: null }));
    await waitFor(() => expect(cardSub()).toMatch(/Fleet/));
    // The picker is gone, not merely unhelpful: pressing Open here would give
    // this DUT a different transport on a cable that is not there.
    expect(openButton()).toBeUndefined();
    const remote = document.querySelector(".conn-remote")?.textContent ?? "";
    expect(remote).toContain("/dev/ttyUSB0");
    expect(remote).toContain("192.168.30.124:22");
  });

  it("does not take the cabled DUT's picker away", async () => {
    await show(dut({ remote: null, serial_open: false, log_path: null, mode: null }));
    expect(document.querySelector(".conn-remote")).toBeNull();
  });
});
