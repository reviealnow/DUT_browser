// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CapabilityReport, CapabilityRow } from "../api/rest";

/**
 * What the status pill claims about one VAP.
 *
 * `✓ match` says the DUT's config and the beacons agree. When no hostapd conf
 * was read, security and PMF are never compared, so a row with no diffs has
 * not been shown to agree; it must say so rather than turn green.
 */

const getCapabilityReport = vi.fn();

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getCapabilityReport: () => getCapabilityReport(),
}));

const { default: SsidCapabilityCard } = await import("./SsidCapabilityCard");

function row(over: Partial<CapabilityRow> = {}): CapabilityRow {
  return {
    iface: "ath0",
    bssid: "02:00:00:00:00:01",
    ssid: "lab",
    band: "5GHz",
    freq_mhz: 5660,
    channel: 132,
    channel_width: "80MHz",
    config_generation: "Wi-Fi 6",
    config_security: "WPA2-Personal",
    config_pmf: "disabled",
    config_dot11k: null,
    config_dot11v: null,
    config_dot11r: null,
    observed_generation: "Wi-Fi 6",
    observed_security: "WPA2-Personal",
    observed_pmf: "disabled",
    observed_dot11k: null,
    observed_dot11v: null,
    observed_dot11r: null,
    observed_signal_dbm: -40,
    match: true,
    diffs: [],
    unknown: [],
    caveat: null,
    ...over,
  };
}

function report(rows: CapabilityRow[]): CapabilityReport {
  return {
    available_b: true,
    scannable_bands: ["5GHz"],
    captured_at_a: "2026-11-06T10:00:00",
    captured_at_b: "2026-11-06T10:00:05",
    rows,
  };
}

afterEach(() => {
  cleanup();
  getCapabilityReport.mockReset();
});

describe("SsidCapabilityCard status pill", () => {
  it("does not show a match for a row whose security was never compared", async () => {
    getCapabilityReport.mockResolvedValue(report([
      row({ iface: "ath0", config_security: null, config_pmf: null, unknown: ["security", "pmf"] }),
      row({ iface: "ath8" }),
    ]));
    render(<SsidCapabilityCard dutId="dut-a" />);

    expect(await screen.findByText("? config unknown")).toBeTruthy();
    // Only ath8 counts as a match; ath0 is counted as unknown.
    expect(screen.getByText("✓ 1 match")).toBeTruthy();
    expect(screen.getByText("? 1 config unknown")).toBeTruthy();
    expect(screen.getAllByText("✓ match")).toHaveLength(1);
  });

  it("still shows a match when nothing was unknown, including older snapshots", async () => {
    const legacy = row({ iface: "ath9" });
    delete legacy.unknown;
    getCapabilityReport.mockResolvedValue(report([row(), legacy]));
    render(<SsidCapabilityCard dutId="dut-b" />);

    expect(await screen.findByText("✓ 2 match")).toBeTruthy();
    expect(screen.getAllByText("✓ match")).toHaveLength(2);
    expect(screen.queryByText(/config unknown/)).toBeNull();
  });

  it("shows a real diff as a diff even when another field is unknown", async () => {
    getCapabilityReport.mockResolvedValue(report([
      row({
        config_security: null,
        config_pmf: null,
        unknown: ["security", "pmf"],
        config_generation: "Wi-Fi 7",
        diffs: [{ field: "generation", label: "Generation", config: "Wi-Fi 7", observed: "Wi-Fi 6" }],
      }),
    ]));
    render(<SsidCapabilityCard dutId="dut-c" />);

    // Once in the summary, once on the row.
    expect(await screen.findAllByText("⚠ 1 diff")).toHaveLength(2);
    expect(screen.queryByText(/config unknown/)).toBeNull();
  });
});
