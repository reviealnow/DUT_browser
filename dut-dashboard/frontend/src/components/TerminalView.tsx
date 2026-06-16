import { useEffect, useRef } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

import { DEFAULT_DUT_ID } from "../api/dut";
import { resizeTerminal } from "../api/rest";

/**
 * Interactive raw serial terminal (xterm.js <-> /ws/term). Forwards keystrokes
 * as bytes and writes raw serial output, so vi/nano work. The backend must
 * already be in terminal mode (POST /api/serial/terminal/enter) — handled by the
 * caller — and this component just carries the byte stream.
 *
 * Offline-first: xterm is bundled locally (no CDN).
 */
export default function TerminalView({ dutId = DEFAULT_DUT_ID }: { dutId?: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }

    const term = new Terminal({
      cursorBlink: true,
      convertEol: false,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      fontSize: 13,
      theme: { background: "#121212", foreground: "#f5f5f5" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    fit.fit();
    term.focus();

    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/term?dut=${dutId}`);
    ws.binaryType = "arraybuffer";

    // Send the DUT its terminal size + TERM once connected, so vi/nano render
    // at the right dimensions. Subsequent resizes (below) update stty only.
    ws.onopen = () => {
      term.writeln("\x1b[2m[connected — interactive serial terminal]\x1b[0m");
      void resizeTerminal(term.rows, term.cols, "xterm");
    };
    ws.onmessage = (event: MessageEvent) => {
      if (event.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(event.data));
      } else if (typeof event.data === "string") {
        term.write(event.data);
      }
    };
    ws.onclose = () => term.writeln("\r\n\x1b[2m[disconnected]\x1b[0m");

    const encoder = new TextEncoder();
    const dataSub = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(encoder.encode(data));
      }
    });

    // When the xterm grid actually changes size, push the new rows/cols to the DUT
    // (debounced so dragging the window doesn't spam stty). onResize only fires on a
    // real grid change, so window-resize -> fit() -> onResize is naturally deduped.
    let resizeTimer: number | undefined;
    const resizeSub = term.onResize(({ rows, cols }) => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        void resizeTerminal(rows, cols);
      }, 150);
    });

    const handleResize = () => {
      try {
        fit.fit();
      } catch {
        // container not measurable yet — ignore.
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      window.clearTimeout(resizeTimer);
      window.removeEventListener("resize", handleResize);
      resizeSub.dispose();
      dataSub.dispose();
      ws.close();
      term.dispose();
    };
  }, [dutId]);

  return (
    <div
      ref={containerRef}
      style={{ height: 420, background: "#121212", padding: 6, borderRadius: 8, overflow: "hidden" }}
    />
  );
}
