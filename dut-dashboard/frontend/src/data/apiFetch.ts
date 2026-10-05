import { IS_DEMO } from "./appMode";
import { demoFetch } from "./demoApi";

/**
 * The one door every backend request goes through. Live mode is plain
 * `fetch`; demo mode is answered in-process by `demoApi.ts` and never reaches
 * the network. Call sites stay mode-agnostic: they get a `Response` either way.
 */
export function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  return IS_DEMO ? demoFetch(input, init) : fetch(input, init);
}
