import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { humanizeApiError } from "../api/rest";
import { DEMO_UNAVAILABLE } from "./appMode";
import { demoFetch } from "./demoApi";

import clientsJson from "../../public/demo/clients.json?raw";
import deviceInfoJson from "../../public/demo/device-info.json?raw";
import systemMonitorJson from "../../public/demo/system-monitor.json?raw";

// The shipped files themselves, not fixtures: these tests are about them.
const PUBLIC_DEMO: Record<string, string> = {
  "clients.json": clientsJson,
  "device-info.json": deviceInfoJson,
  "system-monitor.json": systemMonitorJson,
};

describe("demoFetch", () => {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const name = String(input).split("/demo/")[1];
    return new Response(PUBLIC_DEMO[name], { status: PUBLIC_DEMO[name] ? 200 : 404 });
  });

  beforeEach(() => {
    fetchSpy.mockClear();
    vi.stubGlobal("fetch", fetchSpy);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("never sends an /api request to the network", async () => {
    for (const url of ["/api/duts", "/api/auth/me", "/api/version", "/api/serial/ports", "/api/wifi/clients?dut=default"]) {
      await demoFetch(url);
    }
    await demoFetch("/api/serial/open?dut=default", { method: "POST", body: "{}" });
    const requested = fetchSpy.mock.calls.map(([input]) => String(input));
    expect(requested.every((url) => url.includes("/demo/") && !url.includes("/api/"))).toBe(true);
  });

  it("answers the monitoring routes from the bundled files", async () => {
    const duts = await (await demoFetch("/api/duts")).json();
    expect(duts.duts).toHaveLength(1);
    expect(duts.duts[0]).toMatchObject({ id: "default", serial_open: true, remote: null });

    const clients = await (await demoFetch("/api/wifi/clients?dut=default")).json();
    expect(clients.clients.length).toBeGreaterThan(0);
    expect(typeof clients.captured_at).toBe("string");
  });

  it("browses as an anonymous guest", async () => {
    expect((await demoFetch("/api/auth/me")).status).toBe(401);
  });

  it("refuses DUT actions with copy the UI shows as-is", async () => {
    for (const [url, method] of [
      ["/api/serial/wifi/kick?dut=default", "POST"],
      ["/api/serial/open?dut=default", "POST"],
      ["/api/firmware/upgrade", "POST"],
      ["/api/site-survey?dut=default", "GET"],
    ]) {
      const response = await demoFetch(url, { method });
      expect(response.status).toBe(503);
      expect(humanizeApiError(new Error(await response.text()))).toBe(DEMO_UNAVAILABLE);
    }
  });
});

describe("bundled demo data", () => {
  // The public demo must stay synthetic. These are the identifier shapes the
  // generator promises; a real capture copied in would break at least one.
  const text = Object.values(PUBLIC_DEMO).join("\n");

  it("uses only locally administered demo MACs", () => {
    const macs = text.match(/\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b/gi) ?? [];
    expect(macs.length).toBeGreaterThan(0);
    expect(macs.filter((mac) => !mac.toLowerCase().startsWith("02:00:5e:"))).toEqual([]);
  });

  it("carries no IPv4 address outside TEST-NET-1", () => {
    const ips = text.match(/\b\d{1,3}(?:\.\d{1,3}){3}\b/g) ?? [];
    expect(ips.filter((ip) => !ip.startsWith("192.0.2."))).toEqual([]);
  });
});
