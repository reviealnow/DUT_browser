import { describe, expect, it } from "vitest";

import { LogOrigin } from "../api/rest";
import { describeLogOrigin } from "./logOrigin";

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

/**
 * One cable on the bench carried an AP6_420E in July and an AP6_840E from the
 * 28th, and the Downloads list showed both as `dut-session-<time>.log`.
 */
describe("where a session log came from", () => {
  it("names the unit, the DUT and the host behind a Pi", () => {
    expect(
      describeLogOrigin({
        ...NOTHING,
        mode: "ssh",
        source: "/dev/ttyUSB0",
        dut_id: "pi2-ttyusb0",
        label: "pi2 ttyUSB0",
        host: "192.168.30.124",
        collector_id: "pi2",
        device_id: "AP6420-PA10054DDHWVF2D",
        model: "AP6_420",
      }),
    ).toBe("AP6420-PA10054DDHWVF2D · DUT pi2 ttyUSB0 · via 192.168.30.124 [pi2] /dev/ttyUSB0");
  });

  it("does not let a model pass for a unit", () => {
    // Two AP6_420Es share a model; only the device id tells them apart.
    expect(
      describeLogOrigin({
        ...NOTHING,
        mode: "serial",
        source: "/dev/cu.PL2303G-USBtoUART1130",
        model: "AP6_840E",
      }),
    ).toBe("AP6_840E (unit not identified) · cable /dev/cu.PL2303G-USBtoUART1130");
  });

  it("says a replay is a replay", () => {
    expect(describeLogOrigin({ ...NOTHING, mode: "replay", source: "logs/old.log" })).toBe("replay");
  });

  it("draws nothing for a log that states nothing", () => {
    expect(describeLogOrigin(NOTHING)).toBeNull();
    expect(describeLogOrigin(undefined)).toBeNull();
  });
});
