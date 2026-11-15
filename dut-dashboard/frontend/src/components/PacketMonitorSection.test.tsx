// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PacketAnalysis, PacketHost, PacketMode, PacketRowsPage, WorkspaceFile } from "../api/rest";

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

const { default: PacketMonitorSection, hostBlocker, FILTER_DEBOUNCE_MS, FILES_PAGE, FILES_MAX_PAGES } = await import(
  "./PacketMonitorSection"
);
const { PacketAnalysisProvider, POLL_MS, PAGE_CACHE_MAX, usePacketAnalysis } = await import(
  "../monitoring/PacketAnalysisContext"
);
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

/** A promise the test resolves when it chooses. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const tree = (marker: string) => ({ _source: { layers: { marker } } });

async function runOnce() {
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
}

// Review of #231: what the operator sees when answers arrive late or out of order.
describe("PacketMonitorSection, late and out-of-order answers", () => {
  it("1/2: shows the frame clicked last, not the one answered last", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis.mockResolvedValue(analysis("done"));
    const slow = deferred<unknown>();
    api.getPacketFrame.mockImplementation((_id: number, n: number) =>
      n === 2 ? slow.promise : Promise.resolve(tree("from-frame-1")),
    );
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await runOnce();
    fireEvent.click(await screen.findByRole("button", { name: "#2" }));
    fireEvent.click(screen.getByRole("button", { name: "#1" }));
    expect(await screen.findByText("from-frame-1")).toBeTruthy();
    await act(async () => slow.resolve(tree("from-frame-2")));
    expect(screen.getByText("Frame 1")).toBeTruthy();
    expect(screen.getByText("from-frame-1")).toBeTruthy();
    expect(screen.queryByText("from-frame-2")).toBeNull();
  });

  it("3: a new Run turns 'One row per BSS' off, so a mode without groups shows its rows", async () => {
    api.createPacketAnalysis.mockResolvedValueOnce(1).mockResolvedValueOnce(2);
    api.getPacketAnalysis.mockImplementation((id: number) => Promise.resolve(analysis("done", { id })));
    api.getPacketGroups.mockImplementation((id: number) =>
      Promise.resolve(id === 1 ? [{ group: "aa:bb", frames: 2, row: {}, display: { frame: "1" } }] : []),
    );
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await runOnce();
    fireEvent.click(await screen.findByLabelText(/One row per BSS/));
    await runOnce();
    await waitFor(() => expect(api.getPacketGroups).toHaveBeenCalledWith(2));
    expect(await screen.findAllByText("PSK / SAE")).toHaveLength(2);
    expect(screen.queryByLabelText(/One row per BSS/)).toBeNull();
  });

  it("4: a previous analysis's slow findings do not land on the next one", async () => {
    api.createPacketAnalysis.mockResolvedValueOnce(1).mockResolvedValueOnce(2);
    // A is seen running when created and finishes on a poll, as in the review.
    let firstLookAtA = true;
    api.getPacketAnalysis.mockImplementation((id: number) => {
      if (id === 1 && firstLookAtA) {
        firstLookAtA = false;
        return Promise.resolve(analysis("running", { id }));
      }
      return Promise.resolve(analysis("done", { id }));
    });
    const slowA = deferred<unknown>();
    api.getPacketFindings.mockImplementation((id: number) =>
      id === 1
        ? slowA.promise
        : Promise.resolve([{ rule: "b", severity: "warn", message: "finding of B", group: null, frames: [1] }]),
    );
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await runOnce();
    await waitFor(() => expect(api.getPacketFindings).toHaveBeenCalledWith(1), { timeout: POLL_MS * 3 });
    await waitFor(() => expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(false));
    await runOnce();
    expect(await screen.findByText("finding of B")).toBeTruthy();
    await act(async () =>
      slowA.resolve([{ rule: "a", severity: "fail", message: "finding of A", group: null, frames: [2] }]),
    );
    expect(screen.getByText("finding of B")).toBeTruthy();
    expect(screen.queryByText("finding of A")).toBeNull();
  });

  it("4: polls one at a time on a slow backend, and fetches the results once", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis
      .mockResolvedValueOnce(analysis("running"))
      .mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve(analysis("done")), POLL_MS * 2.5)),
      );
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await runOnce();
    expect(await screen.findByText("SAE but MFPR = 0", {}, { timeout: POLL_MS * 6 })).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, POLL_MS * 2));
    // One create-time GET, then one poll: the ticks while it was slow were skipped.
    expect(api.getPacketAnalysis).toHaveBeenCalledTimes(2);
    expect(api.getPacketFindings).toHaveBeenCalledTimes(1);
  }, 15_000);

  it("5: an edited filter drops the old verdict at once", async () => {
    api.validatePacketFilter.mockImplementation((_host: string, text: string) =>
      Promise.resolve(text === "wlan.nope" ? { ok: false, error: "not a valid protocol" } : { ok: true, error: null }),
    );
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    const input = screen.getByLabelText("Display filter");
    const runButton = () => screen.getByRole("button", { name: "Run" }) as HTMLButtonElement;

    fireEvent.change(input, { target: { value: "wlan.nope" } });
    expect(await screen.findByText("not a valid protocol", {}, { timeout: 3000 })).toBeTruthy();
    fireEvent.change(input, { target: { value: "wlan.ssid" } });
    // The old red error goes with the text it was about.
    expect(screen.queryByText("not a valid protocol")).toBeNull();
    expect(await screen.findByText("Filter is valid.", {}, { timeout: 3000 })).toBeTruthy();
    expect(runButton().disabled).toBe(false);

    // A valid verdict does not carry over to text that has not been checked.
    fireEvent.change(input, { target: { value: "wlan.nope2" } });
    expect(runButton().disabled).toBe(true);
    expect(screen.queryByText("Filter is valid.")).toBeNull();
  });

  it("6: a failed poll's error goes once a later poll works", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis
      .mockResolvedValueOnce(analysis("running"))
      .mockRejectedValueOnce(new Error('{"detail":"Backend restarting."}'))
      .mockResolvedValue(analysis("done", { warning: "The capture is cut short." }));
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await runOnce();
    expect(await screen.findByText("Backend restarting.", {}, { timeout: POLL_MS * 3 })).toBeTruthy();
    expect(await screen.findByText("Done: 2 frames.", {}, { timeout: POLL_MS * 3 })).toBeTruthy();
    expect(screen.getByText("The capture is cut short.")).toBeTruthy();
    expect(screen.queryByText("Backend restarting.")).toBeNull();
  });

  it("6: a Cancel that lost the race to the end shows the result, not an error", async () => {
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis.mockResolvedValueOnce(analysis("running")).mockResolvedValue(analysis("done"));
    api.cancelPacketAnalysis.mockRejectedValue(new Error('{"detail":"Analysis 1 has already finished."}'));
    renderSection();
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await runOnce();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Done: 2 frames.")).toBeTruthy();
    expect(await screen.findByText("SAE but MFPR = 0")).toBeTruthy();
    expect(screen.queryByText("Analysis 1 has already finished.")).toBeNull();
  });
});

describe("PacketTable", () => {
  it("10: words a failed page the way the rest of the section does", async () => {
    const page = vi.fn(() => Promise.reject(new Error('{"detail":"Analysis 1 is not done."}')));
    render(
      <PacketTable
        total={10} columns={COLUMNS} page={page}
        severityByFrame={new Map()} selectedFrame={null} onOpen={() => {}}
      />,
    );
    expect(await screen.findByText("Analysis 1 is not done.")).toBeTruthy();
  });

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

// Review of #231, points 7-9.
describe("PacketMonitorSection, review of #231 points 7-9", () => {
  /** The section behind a switch, as AppShell mounts it under the provider. */
  function Navigable() {
    const [shown, setShown] = useState(true);
    return (
      <PacketAnalysisProvider>
        <button type="button" onClick={() => setShown((s) => !s)}>
          navigate
        </button>
        {shown ? <PacketMonitorSection /> : null}
      </PacketAnalysisProvider>
    );
  }

  it("7: leaving the section and coming back keeps the form behind the results", async () => {
    const second: PacketMode = { ...MODES[0], id: "deauth", label: "Deauth storm", description: "Deauths." };
    api.getPacketModes.mockResolvedValue([MODES[0], second]);
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis.mockResolvedValue(analysis("done", { mode_id: "deauth" }));
    api.getPacketGroups.mockResolvedValue([{ group: "aa:bb", frames: 2, row: {}, display: { frame: "1" } }]);
    api.getPacketFrame.mockResolvedValue(tree("frame-2-tree"));
    render(<Navigable />);
    await choose("Capture file", "3");
    await choose("Host", "pi");
    await choose("Test Mode", "deauth");
    fireEvent.change(screen.getByLabelText("Display filter"), { target: { value: "wlan.ssid" } });
    fireEvent.change(screen.getByLabelText("Extra fields"), { target: { value: "wlan.fixed.beacon" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(false), {
      timeout: 3000,
    });
    await runOnce();
    fireEvent.click(await screen.findByRole("button", { name: "#2" }));
    expect(await screen.findByText("frame-2-tree")).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/One row per BSS/));

    fireEvent.click(screen.getByRole("button", { name: "navigate" }));
    expect(screen.queryByLabelText("Capture file")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "navigate" }));

    const value = (label: string) => (screen.getByLabelText(label) as HTMLInputElement | HTMLSelectElement).value;
    await waitFor(() => expect(value("Capture file")).toBe("3"));
    expect(value("Host")).toBe("pi");
    expect(value("Test Mode")).toBe("deauth");
    expect(value("Display filter")).toBe("wlan.ssid");
    expect(value("Extra fields")).toBe("wlan.fixed.beacon");
    expect((screen.getByLabelText(/One row per BSS/) as HTMLInputElement).checked).toBe(true);
    // The open frame comes back with its tree, fetched again.
    expect(await screen.findByText("frame-2-tree")).toBeTruthy();
    expect(screen.getByText("Frame 2")).toBeTruthy();
  });

  it("8: offers captures beyond the newest 500 files, by paging the filename search", async () => {
    const pcaps: WorkspaceFile[] = Array.from({ length: 1200 }, (_, i) => ({
      id: 10_000 - i, filename: `run-${i}.pcapng`, size: 1, uploader: null,
      uploaded_at: `2026-11-${String(12 - Math.floor(i / 200)).padStart(2, "0")}T00:00:00`,
    })) as WorkspaceFile[];
    const old = { id: 7, filename: "legacy.CAP", size: 1, uploader: null, uploaded_at: "2020-01-01T00:00:00" };
    api.getFiles.mockImplementation(({ q, offset = 0, limit }: { q?: string; offset?: number; limit?: number }) => {
      const all = q === ".pcap" ? pcaps : q === ".cap" ? [old] : [];
      return Promise.resolve({ files: all.slice(offset, offset + (limit ?? all.length)), stats: {}, total: all.length });
    });
    renderSection();
    const select = (await screen.findByLabelText("Capture file")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(1 + 1200 + 1));
    expect(api.getFiles).toHaveBeenCalledWith({ q: ".pcap", limit: FILES_PAGE, offset: 1000 });
    expect(select.options[1].textContent).toBe("run-0.pcapng");
    expect(select.options[select.options.length - 1].textContent).toBe("legacy.CAP");
    expect(screen.queryByText(/only the newest are listed/)).toBeNull();
  });

  it("8: says so when the capture list may be incomplete", async () => {
    let next = 1;
    api.getFiles.mockImplementation(({ q, limit = FILES_PAGE }: { q?: string; limit?: number }) =>
      Promise.resolve({
        files: q === ".pcap" ? Array.from({ length: limit }, () => ({ id: next++, filename: "x.pcap", size: 1, uploader: null, uploaded_at: "" })) : [],
        stats: {},
        total: q === ".pcap" ? 1_000_000 : 0,
      }),
    );
    renderSection();
    expect(await screen.findByText(/only the newest are listed/)).toBeTruthy();
    expect(api.getFiles.mock.calls.filter(([o]) => o.q === ".pcap")).toHaveLength(FILES_MAX_PAGES);
  });

  it("9: the context keeps a bounded number of row pages, and asks once per page in flight", async () => {
    let ctx!: ReturnType<typeof usePacketAnalysis>;
    function Grab() {
      ctx = usePacketAnalysis();
      return null;
    }
    api.createPacketAnalysis.mockResolvedValue(1);
    api.getPacketAnalysis.mockResolvedValue(analysis("done"));
    render(
      <PacketAnalysisProvider>
        <Grab />
      </PacketAnalysisProvider>,
    );
    await act(() => ctx.start({ file_id: 3, collector_id: "pi", mode_id: "beacon_security", display_filter: "", extra_fields: [] }));
    await waitFor(() => expect(ctx.analysis?.id).toBe(1));

    const first = deferred<PacketRowsPage>();
    api.getPacketRows.mockImplementation((_id: number, offset: number, limit: number) =>
      offset === 0 ? first.promise : Promise.resolve(rows(offset, limit, 100_000)),
    );
    const a = ctx.page(0, PAGE_SIZE);
    const b = ctx.page(0, PAGE_SIZE);
    expect(api.getPacketRows).toHaveBeenCalledTimes(1);
    first.resolve(rows(0, PAGE_SIZE, 100_000));
    await Promise.all([a, b]);
    await ctx.page(0, PAGE_SIZE);
    expect(api.getPacketRows).toHaveBeenCalledTimes(1);

    for (let n = 1; n <= PAGE_CACHE_MAX; n += 1) {
      await ctx.page(n * PAGE_SIZE, PAGE_SIZE);
    }
    expect(api.getPacketRows).toHaveBeenCalledTimes(1 + PAGE_CACHE_MAX);
    // The first page was the least recently used, so it is gone and asked for again.
    await ctx.page(0, PAGE_SIZE);
    expect(api.getPacketRows).toHaveBeenCalledTimes(2 + PAGE_CACHE_MAX);
    expect(api.getPacketRows).toHaveBeenLastCalledWith(1, 0, PAGE_SIZE);
  });

  it("9: the table drops pages the window has left, and asks again on the way back", async () => {
    const total = 50_000;
    const page = vi.fn((offset: number, limit: number) => Promise.resolve(rows(offset, limit, total)));
    render(
      <PacketTable
        total={total} columns={COLUMNS} page={page}
        severityByFrame={new Map()} selectedFrame={null} onOpen={() => {}}
      />,
    );
    await screen.findByText("1");
    const view = screen.getByTestId("packet-window");
    fireEvent.scroll(view, { target: { scrollTop: 30_000 * ROW_HEIGHT } });
    await screen.findByText("30001");
    expect(screen.queryByText("1")).toBeNull();
    fireEvent.scroll(view, { target: { scrollTop: 0 } });
    await screen.findByText("1");
    expect(page.mock.calls.filter(([offset]) => offset === 0)).toHaveLength(2);
  });
});
