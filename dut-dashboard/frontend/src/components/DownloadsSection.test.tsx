// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LogList, LogOrigin } from "../api/rest";

/**
 * A session row says where its log came from.
 *
 * The filename carries a label and a time and nothing else, so two logs from
 * one cable -- an AP6_420E in July, an AP6_840E from the 28th -- used to be
 * told apart only by the time. The origin comes from the log itself.
 */

const getLogs = vi.fn();

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getLogs: () => getLogs(),
}));

const { default: DownloadsSection } = await import("./DownloadsSection");

const NOTHING: LogOrigin = {
  mode: null,
  source: null,
  dut_id: null,
  label: null,
  host: null,
  collector_id: null,
  device_id: null,
  model: null,
};

function listing(origin: LogOrigin): LogList {
  return {
    sessions: [
      { name: "dut-session-pi2ttyUSB0-20260916-115810.log", size: 2048, mtime: "2026-09-16T12:10:00", context: [], origin },
    ],
    artifacts: [],
    surveys: [],
    context: [],
  };
}

afterEach(() => {
  cleanup();
  getLogs.mockReset();
});

describe("the session log table", () => {
  it("names the unit and the host a log was recorded from", async () => {
    getLogs.mockResolvedValue(
      listing({
        ...NOTHING,
        mode: "ssh",
        source: "/dev/ttyUSB0",
        label: "pi2 ttyUSB0",
        host: "192.168.30.124",
        collector_id: "pi2",
        device_id: "AP6420-PA10054DDHWVF2D",
      }),
    );
    render(<DownloadsSection />);
    expect(
      await screen.findByText("AP6420-PA10054DDHWVF2D · DUT pi2 ttyUSB0 · via 192.168.30.124 [pi2] /dev/ttyUSB0"),
    ).toBeTruthy();
  });

  it("adds no line for a log that states nothing", async () => {
    getLogs.mockResolvedValue(listing(NOTHING));
    const { container } = render(<DownloadsSection />);
    await screen.findByText("dut-session-pi2ttyUSB0-20260916-115810.log", { exact: false });
    // Only the context note: an empty origin line would be a blank row of
    // faint text reading as "something here failed to load".
    expect(container.querySelectorAll("td.filetable-name .context-note")).toHaveLength(1);
  });
});
