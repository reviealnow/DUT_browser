// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEMO_UNAVAILABLE } from "../data/appMode";

/**
 * `/ws/term` is the one socket in the app that is not behind the data layer:
 * the terminal carries raw serial bytes, so it never went through
 * `IDataSource`. In Demo Mode nothing may reach a backend, and a guest cannot
 * open the terminal today (the Serial Console is gated, and entering terminal
 * mode is a REST call the demo refuses) -- but that is two facts about other
 * files. This pins the component's own behaviour, so a future path that mounts
 * it in a demo still opens no socket.
 */

let demo = false;
vi.mock("../data/appMode", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../data/appMode")>()),
  get IS_DEMO() {
    return demo;
  },
}));

const written: string[] = [];
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    rows = 24;
    cols = 80;
    loadAddon() {}
    open() {}
    focus() {}
    dispose() {}
    write(text: string) {
      written.push(text);
    }
    writeln(text: string) {
      written.push(text);
    }
    onData() {
      return { dispose() {} };
    }
    onResize() {
      return { dispose() {} };
    }
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));

import TerminalView from "./TerminalView";

describe("TerminalView", () => {
  const sockets: string[] = [];

  beforeEach(() => {
    sockets.length = 0;
    written.length = 0;
    vi.stubGlobal(
      "WebSocket",
      class {
        static OPEN = 1;
        readyState = 0;
        binaryType = "";
        constructor(url: string) {
          sockets.push(url);
        }
        send() {}
        close() {}
      },
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("opens /ws/term for the selected DUT in live mode", () => {
    demo = false;
    render(<TerminalView dutId="bench-2" />);
    expect(sockets).toHaveLength(1);
    expect(sockets[0]).toMatch(/\/ws\/term\?dut=bench-2$/);
  });

  it("opens no socket in Demo Mode, and says why in the terminal", () => {
    demo = true;
    render(<TerminalView dutId="default" />);
    expect(sockets).toEqual([]);
    expect(written.join("\n")).toContain(DEMO_UNAVAILABLE);
  });
});
