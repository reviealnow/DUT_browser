import type { DutInfo, WifiClientsResult } from "../api/rest";
import { DEFAULT_CRASH_KEYWORDS } from "../monitoring/crash";
import { DEMO_UNAVAILABLE } from "./appMode";
import { DEMO_DEVICE_ID, DEMO_DUT_ID, demoAssetUrl, formatDeviceTs } from "./ReplayDataSource";

/**
 * Demo mode's answer to every REST call, given without touching the network.
 *
 * `api/rest.ts` sends all its requests through `apiFetch`, which hands them
 * here when the build is a demo. A short list of read-only routes the
 * monitoring screens need is answered from the bundled synthetic files; every
 * other route — anything that would drive the DUT, write to the workspace or
 * read a capture this demo does not carry — gets a 503 whose `detail` says so.
 * The UI already turns `detail` into its error copy (`humanizeApiError`), so a
 * refused action reads as "not available in Demo Mode" rather than as a broken
 * connection, and no request ever leaves for `localhost:8000`.
 *
 * The session is anonymous (`/api/auth/me` answers 401, which the app treats
 * as a normal guest browser), so the demo shows what a guest sees and no more:
 * the role gates in `navigation.ts` hide the engineer and admin sections
 * exactly as they would on the bench.
 */
export async function demoFetch(input: string, init?: RequestInit): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const url = new URL(input, "http://demo.invalid");
  if (method === "GET") {
    const handler = GET_ROUTES[url.pathname];
    if (handler) {
      return handler(url);
    }
  }
  return json(503, { detail: DEMO_UNAVAILABLE });
}

type Handler = (url: URL) => Promise<Response> | Response;

const GET_ROUTES: Record<string, Handler> = {
  "/api/auth/me": () => json(401, { detail: "Not signed in" }),
  "/api/duts": async () => json(200, { duts: [await demoDut()] }),
  "/api/settings/crash-keywords": () => json(200, { keywords: DEFAULT_CRASH_KEYWORDS }),
  "/api/wifi/clients": async (url) => {
    if ((url.searchParams.get("dut") ?? DEMO_DUT_ID) !== DEMO_DUT_ID) {
      return json(404, { detail: "Unknown DUT" });
    }
    const file = await asset<{ clients: WifiClientsResult["clients"]; vaps: WifiClientsResult["vaps"] }>(
      "clients.json",
    );
    const result: WifiClientsResult = {
      clients: file.clients,
      vaps: file.vaps,
      captured_at: formatDeviceTs(Date.now()),
    };
    return json(200, result);
  },
};

type DeviceInfoFile = {
  dut: Pick<DutInfo, "id" | "label" | "model" | "model_cores" | "device_id" | "vaps_per_band" | "bands">;
};

async function demoDut(): Promise<DutInfo> {
  const { dut } = await asset<DeviceInfoFile>("device-info.json");
  return {
    ...dut,
    id: DEMO_DUT_ID,
    device_id: dut.device_id ?? DEMO_DEVICE_ID,
    // A replay is what this is, and "open" is what makes the cards read as a
    // live session; neither is a claim about a cable.
    mode: "replay",
    serial_open: true,
    log_path: null,
    removable: false,
    mgmt_url: "",
    last_serial: null,
    remote: null,
    backhaul: {
      applicable: false,
      captured: false,
      console_id: "demo",
      role: null,
      uplink: null,
      downlink: null,
    },
    mesh_probe: null,
  };
}

const assetCache = new Map<string, Promise<unknown>>();

function asset<T>(name: string): Promise<T> {
  let pending = assetCache.get(name);
  if (!pending) {
    pending = fetch(demoAssetUrl(name)).then((response) => {
      if (!response.ok) {
        assetCache.delete(name);
        throw new Error(`HTTP ${response.status} for demo/${name}`);
      }
      return response.json();
    });
    assetCache.set(name, pending);
  }
  return pending as Promise<T>;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
