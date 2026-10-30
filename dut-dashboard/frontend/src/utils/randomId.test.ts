import { afterEach, describe, expect, it, vi } from "vitest";

import { randomId } from "./randomId";

/**
 * `crypto.randomUUID` exists only in a secure context, and this project's main
 * deployment is a plain-HTTP LAN origin. There it is `undefined`, and calling it
 * made every Offline Analyzer import fail as "The logs could not be saved".
 */
const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("randomId", () => {
  it("returns a v4 UUID where randomUUID is unavailable", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
    });

    const first = randomId();
    const second = randomId();

    expect(first).toMatch(V4);
    expect(second).toMatch(V4);
    expect(first).not.toBe(second);
  });

  it("uses randomUUID where the browser offers it", () => {
    const real = globalThis.crypto;
    vi.stubGlobal("crypto", {
      getRandomValues: real.getRandomValues.bind(real),
      randomUUID: () => "00000000-0000-4000-8000-000000000000",
    });

    expect(randomId()).toBe("00000000-0000-4000-8000-000000000000");
  });
});
