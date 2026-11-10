// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PacketAnalysis, PacketHost, PacketMode, PacketRowsPage } from "../api/rest";

/**
 * Packet Monitor's section (docs/design-packet-monitor.md §B11): what it asks
 * the backend, when, and what it refuses to let the user start.
 */

const api = {
  getFiles: vi.fn(),
  getPacketHosts: vi.fn(),
  getPacketModes: vi.fn(),
  validatePacketFilter: vi.fn(),
  createPacketAnalysis: vi.fn(),
  getPacketAnalysis: vi.fn(),
  getPacketFindings: vi.fn(),
  getPacketGroups: vi.fn(),
  getPacketRows: vi.fn(),
  cancelPacketAnalysis: vi.fn(),
  probePacketHost: vi.fn(),
  getPacketFrame: vi.fn(),
};

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  ...Object.fromEntries(Object.entries(api).map(([name, fn]) => [name, (...args: unknown[]) => fn(...args)])),
}));

const { default: PacketMonitorSection, hostBlocker, FILTER_DEBOUNCE_MS } = await import("./PacketMonitorSection");
const { PacketAnalysisProvider, POLL_MS } = await import("../monitoring/PacketAnalysisContext");
const { PacketTable, ROW_HEIGHT, PAGE_SIZE } = await import("./PacketTable");

const PROBE = {
  collector_id: "pi", probed_at: 0, os_name: "Linux", tshark_path: "/usr/bin/tshark",
  tshark_version: "4.4.19", checksum_tool: "sha256sum", base64_flag: "-d", free_bytes: 2e10,
  staged: {}, swept: [], killed: [], problems: [], install_hint: null, over_budget: false, selectable: true,
};
const READY: PacketHost = { id: "pi", label: "Bench Pi", connected: true, probe: PROBE, selectable: true };
const MODES: PacketMode[] = [
  {
    id: "beacon_security", label: "Single Beacon Monitor", description: "RSN per BSS.",
    display_filter: "wlan.fc.type_subtype == 0x0008", group_by: "bssid",
    columns: [{ key: "frame", label: "No.", decoder: "int", hidden: false }], rules: [],
  },
];
const COLUMNS = [
  { key: "frame", label: "No.", decoder: "int", hidden: false },
  { key: "akm", label: "RSN AKM", decoder: "akm_list", hidden: false },
];

function analysis(status: PacketAnalysis["status"], extra: Partial<PacketAnalysis> = {}): PacketAnalysis {
  return {
    id: 1, file_id: 3, file_sha256: "a".repeat(64), collector_id: "pi", mode_id: "beacon_security",
    display_filter: "", extra_fields: [], status, error: null, warning: null, frames_total: null,
    frames_shown: status === "done" ? 2 : null, rows_dropped: 0, tshark_version: "4.4.19",
    created_by_user_id: 1, created_at: "", finished_at: null,
    findings_summary: { fail: 1, warn: 0, info: 1 }, columns: status === "done" ? COLUMNS : null, ...extra,
  };
}

function rows(offset: number, limit: number, total: number): PacketRowsPage {
  const count = Math.max(0, Math.min(limit, total - offset));
  const page = Array.from({ length: count }, (_, i) => ({ frame: offset + i + 1, akm: [2, 8] }));
  return {
    offset, limit, total, rows: page,
    display: page.map((r) => ({ frame: String(r.frame), akm: "PSK / SAE" })),
  };
}

function renderSection() {
  return render(
    <PacketAnalysisProvider>
      <PacketMonitorSection />
    </PacketAnalysisProvider>,
  );
}

async function choose(label: string, value: string) {
  const select = (await screen.findByLabelText(label)) as HTMLSelectElement;
  await waitFor(() => expect([...select.options].some((o) => o.value === value)).toBe(true));
  fireEvent.change(select, { target: { value } });
}

beforeEach(() => {
  localStorage.clear();
  api.getFiles.mockResolvedValue({
    files: [
      { id: 3, filename: "beacons.pcapng", size: 1, uploader: null, uploaded_at: "" },
      { id: 4, filename: "notes.pdf", size: 1, uploader: null, uploaded_at: "" },
    ],
    stats: {}, total: 2,
  });
  api.getPacketHosts.mockResolvedValue([READY]);
  api.getPacketModes.mockResolvedValue(MODES);
  api.validatePacketFilter.mockResolvedValue({ ok: true, error: null });
  api.getPacketFindings.mockResolvedValue([
    { rule: "wpa3_requires_mfpr", severity: "fail", message: "SAE but MFPR = 0", group: "aa:bb", frames: [2] },
    { rule: "hidden_ssid", severity: "info", message: "Hidden SSID", group: "aa:bb", frames: [1] },
  ]);
  api.getPacketGroups.mockResolvedValue([]);
  api.getPacketRows.mockImplementation((_id: number, offset: number, limit: number) => Promise.resolve(rows(offset, limit, 2)));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Object.values(api).forEach((fn) => fn.mockReset());
});

describe("hostBlocker", () => {
  it("names what is in the way, in the operator's terms", () => {
    expect(hostBlocker(READY)).toBe("");
    expect(hostBlocker({ ...READY, connected: false })).toContain("ask an admin to connect it");
    expect(hostBlocker({ ...READY, probe: null })).toBe("not probed yet");
    const missing = { ...PROBE, problems: ["tshark not installed"], install_hint: "sudo apt install tshark", selectable: false };
    expect(hostBlocker({ ...READY, probe: missing, selectable: false })).toBe(
      "tshark not installed (sudo apt install tshark)",
    );
  });
});

describe("PacketMonitorSection", () => {
  it("offers only captures from the Files workspace", async () => {
    renderSection();
    const select = (await screen.findByLabelText("Capture file")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));
    expect([...select.options].map((o) => o.textContent)).toEqual(["Choose a capture", "beacons.pcapng"]);
  });

  it("validates the filter once, after the user stops typing", async () => {
    renderSection();
    await choose("Host", "pi");
    vi.useFakeTimers();
    const input = screen.getByLabelText("Display filter");
    fireEvent.change(input, { target: { value: "wlan" } });
    fireEvent.change(input, { target: { value: "wlan.ssid" } });
    await act(async () => {
      vi.advanceTimersByTime(FILTER_DEBOUNCE_MS - 1);
    });
    expect(api.validatePacketFilter).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(api.validatePacketFilter).toHaveBeenCalledTimes(1);
    expect(api.validatePacketFilter).toHaveBeenCalledWith("pi", "wlan.ssid");
  });

  it("will not run with a filter tshark rejected", async () => {
    api.validatePacketFilter.mockResolvedValue({ ok: false, error: '"wlan.nope" is not a valid protocol' });
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    fireEvent.change(screen.getByLabelText("Display filter"), { target: { value: "wlan.nope" } });
    expect(await screen.findByText('"wlan.nope" is not a valid protocol', {}, { timeout: 3000 })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("will not run on a Host that is not ready, and says why", async () => {
    api.getPacketHosts.mockResolvedValue([
      { ...READY, probe: { ...PROBE, problems: ["tshark 3.4.16 is older than 3.6"], selectable: false }, selectable: false },
    ]);
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    expect(await screen.findByText(/tshark 3\.4\.16 is older than 3\.6/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("runs, polls while running, then shows findings and rows", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis
      .mockResolvedValueOnce(analysis("running"))
      .mockResolvedValue(analysis("done"));
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.createPacketAnalysis).toHaveBeenCalledTimes(1));
    expect(api.createPacketAnalysis.mock.calls[0][0]).toEqual({
      file_id: 3, collector_id: "pi", mode_id: "beacon_security", display_filter: "", extra_fields: [],
    });
    expect(await screen.findByText("Running on the Host…")).toBeTruthy();
    expect(await screen.findByText("SAE but MFPR = 0", {}, { timeout: POLL_MS * 3 })).toBeTruthy();
    expect(await screen.findAllByText("PSK / SAE")).toHaveLength(2);
    // The poll stops once the analysis is done.
    const polls = api.getPacketAnalysis.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS * 1.5));
    expect(api.getPacketAnalysis.mock.calls.length).toBe(polls);
  });

  it("filters the findings by severity", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis.mockResolvedValue(analysis("done"));
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Hidden SSID")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "1 fail" }));
    expect(screen.queryByText("Hidden SSID")).toBeNull();
    expect(screen.getByText("SAE but MFPR = 0")).toBeTruthy();
  });

  it("shows a truncated capture's warning", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis.mockResolvedValue(analysis("done", { warning: "The capture is cut short." }));
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("The capture is cut short.")).toBeTruthy();
  });
});

describe("PacketTable", () => {
  it("keeps only the visible rows in the DOM and fetches pages as it scrolls", async () => {
    const total = 50_000;
    const page = vi.fn((offset: number, limit: number) => Promise.resolve(rows(offset, limit, total)));
    render(
      <PacketTable
        total={total} columns={COLUMNS} page={page}
        severityByFrame={new Map([[2, "fail" as const]])} selectedFrame={null} onOpen={() => {}}
      />,
    );
    await screen.findAllByText("PSK / SAE");
    expect(page).toHaveBeenCalledTimes(1);
    expect(page).toHaveBeenCalledWith(0, PAGE_SIZE);
    expect(document.querySelectorAll("tr.pkt-row").length).toBeLessThan(60);
    expect(document.querySelector("tr.pkt-fail")).not.toBeNull();

    const view = screen.getByTestId("packet-window");
    fireEvent.scroll(view, { target: { scrollTop: 30_000 * ROW_HEIGHT } });
    await waitFor(() => expect(page).toHaveBeenCalledWith(30_000, PAGE_SIZE));
    await screen.findByText("30001");
    expect(document.querySelectorAll("tr.pkt-row").length).toBeLessThan(60);
  });
});
