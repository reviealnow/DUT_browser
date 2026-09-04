import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DEFAULT_DUT_ID } from "../api/dut";
import {
  closeSerial,
  enterTerminal,
  exitTerminal,
  getSerialLogDownloadUrl,
  humanizeApiError,
  listSerialPorts,
  openSerial,
  sendSerial,
  SerialPortInfo,
} from "../api/rest";
import ConsolePanel from "../components/ConsolePanel";
// Lazy-loaded so the xterm.js bundle only loads when the terminal is opened.
const TerminalView = lazy(() => import("../components/TerminalView"));
import { useDutMonitorContext } from "../monitoring/DutMonitorContext";
import { useCrashKeywords } from "../monitoring/useCrashKeywords";
import { loadSettings } from "../monitoring/useSettings";
const DEFAULT_SERIAL_PORT = "/dev/ttyUSB0";

function choosePreferredPort(ports: SerialPortInfo[]): string {
  if (ports.length === 0) {
    return "";
  }
  const macosCuPort = ports.find((portInfo) => portInfo.device.startsWith("/dev/cu."));
  return macosCuPort ? macosCuPort.device : ports[0].device;
}

export default function Dashboard({
  active = true,
  dutId = DEFAULT_DUT_ID,
  onSerialOpened,
}: {
  active?: boolean;
  dutId?: string;
  /** Fired once a real serial connection opens (not replay) — lets the shell
   *  kick off a background site survey the moment the DUT connects. */
  onSerialOpened?: (dutId: string) => void;
}) {
  // Console lines come from the single shared WebSocket (useDutMonitor) instead
  // of Dashboard opening its own connection.
  const { lines, linesStartSeq, serialDisconnect } = useDutMonitorContext();
  const [mode, setMode] = useState<"serial" | "replay">("serial");
  const [port, setPort] = useState(DEFAULT_SERIAL_PORT);
  const [baudrate, setBaudrate] = useState(() => loadSettings().defaultBaud);
  // Free-text label that names this DUT's session log (dut-session-<label>-<ts>.log)
  // so logs from different DUTs are identifiable. Required to Open in serial mode.
  const [dutLabel, setDutLabel] = useState("");
  const [replayPath, setReplayPath] = useState("logs/sample.log");
  const [replayIntervalMs, setReplayIntervalMs] = useState(100);
  const [serialPorts, setSerialPorts] = useState<SerialPortInfo[]>([]);
  const [portsLoading, setPortsLoading] = useState(false);
  const [portsError, setPortsError] = useState("");
  const [currentLogFileName, setCurrentLogFileName] = useState("");
  const [consoleView, setConsoleView] = useState<"monitor" | "terminal">("monitor");
  const [isOpen, setIsOpen] = useState(false);
  const [actionError, setActionError] = useState("");
  const [lastSeenCriticalCrashCount, setLastSeenCriticalCrashCount] = useState(0);
  const [criticalCrashKeywordInput, setCriticalCrashKeywordInput] = useState("");
  const { keywords: lockedCriticalCrashKeywords, pattern: crashPattern, saving: keywordSaving, saveKeywords } = useCrashKeywords();
  const [downloadNotice, setDownloadNotice] = useState<{ message: string; tone: "blue" | "green" } | null>(null);

  // Run an action; on failure show friendly copy (never raw JSON) in the banner.
  // `silent` suppresses the banner for implicit actions (e.g. a stray Ctrl-C while
  // the port is closed), which would otherwise be noisy.
  async function runAction(fn: () => Promise<void>, silent = false): Promise<void> {
    try {
      await fn();
      setActionError("");
    } catch (error) {
      if (!silent) {
        setActionError(humanizeApiError(error));
      }
    }
  }

  async function handleOpen() {
    await runAction(async () => {
      const response = await openSerial({
        mode,
        port,
        baudrate,
        replay_path: mode === "replay" ? replayPath : undefined,
        replay_interval_ms: replayIntervalMs,
        session_label: dutLabel.trim() || undefined,
      }, dutId);
      const logPath = response.log_path || "";
      const fileName = logPath.split(/[\\/]/).pop() || "";
      setCurrentLogFileName(fileName);
      setIsOpen(true);
      // A real serial connection just came up — prescan the site survey now so
      // it's ready regardless of which page the user is on. Replay has no serial
      // to scan, so skip it there.
      if (mode === "serial") {
        onSerialOpened?.(dutId);
      }
    });
  }

  async function handleClose() {
    await runAction(async () => {
      await closeSerial(dutId);
      setIsOpen(false);
    });
  }

  async function handleSend(text: string, silent = false) {
    // A bare Ctrl-C (ETX) arrives from ConsolePanel's global key handler; don't
    // nag with a banner when the port isn't open. The explicit Stop button (via
    // handleStopCommand) still warns.
    const quiet = silent || text === String.fromCharCode(3);
    await runAction(() => sendSerial(text, dutId).then(() => undefined), quiet);
  }

  // The backend pushes this when the serial device vanishes under it (adapter
  // unplugged, DUT rebooted, port re-enumerated). It deliberately does not
  // reconnect — a re-enumerated adapter may be a different device — so the
  // session really is over: drop out of the connected view and say so, rather
  // than leaving "Connected" up while every keystroke fails.
  useEffect(() => {
    if (!serialDisconnect) {
      return;
    }
    setIsOpen(false);
    setConsoleView("monitor");
    setActionError("Serial device disconnected. Reconnect it, then press Open again.");
  }, [serialDisconnect]);

  async function handleOpenTerminal() {
    await runAction(async () => {
      await enterTerminal(dutId); // backend switches to raw mode; sysmon monitoring pauses
      setConsoleView("terminal");
    });
  }

  function handleCloseTerminal() {
    // Unmount the terminal first (closes /ws/term), then resume monitoring.
    setConsoleView("monitor");
    void exitTerminal(dutId);
  }

  // Dashboard now stays mounted across nav (state persists). When the Serial
  // Console section is hidden while in terminal mode, auto-exit terminal so the
  // other sections' KPIs/charts keep updating (monitoring resumes), and return
  // to the monitor view; the serial session and selected port are preserved.
  useEffect(() => {
    if (!active && consoleView === "terminal") {
      setConsoleView("monitor");
      void exitTerminal(dutId);
    }
  }, [active, consoleView, dutId]);

  // Switching the selected DUT: leave terminal mode on the DUT we're leaving
  // (free it) and clear the per-session log name so Download reflects only the
  // newly-selected DUT's session.
  const prevDutRef = useRef(dutId);
  useEffect(() => {
    if (prevDutRef.current !== dutId) {
      if (consoleView === "terminal") {
        setConsoleView("monitor");
        void exitTerminal(prevDutRef.current);
      }
      setCurrentLogFileName("");
      prevDutRef.current = dutId;
    }
  }, [dutId, consoleView]);


  async function handleRunTop() {
    await runAction(() => sendSerial("top\n", dutId).then(() => undefined));
  }

  async function handleStopCommand() {
    await sendSerial("\u0003", dutId);
  }

  function parseDownloadFileName(contentDisposition: string | null, fallbackName: string): string {
    if (!contentDisposition) {
      return fallbackName;
    }
    const utf8Match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match?.[1]) {
      try {
        return decodeURIComponent(utf8Match[1]);
      } catch {
        return utf8Match[1];
      }
    }
    const asciiMatch = contentDisposition.match(/filename=\"?([^\";]+)\"?/i);
    return asciiMatch?.[1] || fallbackName;
  }

  async function handleDownloadLog() {
    if (!currentLogFileName) {
      return;
    }
    const fallbackName = currentLogFileName;
    const response = await fetch(getSerialLogDownloadUrl(currentLogFileName));
    if (!response.ok) {
      throw new Error(await response.text());
    }

    const contentType = (response.headers.get("content-type") || "").toLowerCase();
    const fileName = parseDownloadFileName(response.headers.get("content-disposition"), fallbackName);
    const blob = await response.blob();

    const url = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.URL.revokeObjectURL(url);

    if (contentType.includes("text/plain")) {
      setDownloadNotice({ message: "The log file is ready.", tone: "blue" });
      return;
    }
    setDownloadNotice({ message: "DUT CPU and Memory usage plots are created.", tone: "green" });
  }

  useEffect(() => {
    if (!downloadNotice) {
      return;
    }
    const timer = window.setTimeout(() => {
      setDownloadNotice(null);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [downloadNotice]);

  function handleLockCriticalCrashKeyword() {
    const keyword = criticalCrashKeywordInput.trim();
    if (!keyword) return;
    if (lockedCriticalCrashKeywords.some((k) => k.toLowerCase() === keyword.toLowerCase())) {
      setCriticalCrashKeywordInput("");
      return;
    }
    void saveKeywords([...lockedCriticalCrashKeywords, keyword]);
    setCriticalCrashKeywordInput("");
  }

  function handleRemoveCriticalCrashKeyword(keywordToRemove: string) {
    void saveKeywords(lockedCriticalCrashKeywords.filter((k) => k.toLowerCase() !== keywordToRemove.toLowerCase()));
  }

  const refreshSerialPorts = useCallback(async () => {
    setPortsLoading(true);
    setPortsError("");
    try {
      const ports = await listSerialPorts();
      setSerialPorts(ports);
      if (ports.length > 0) {
        const preferredPort = choosePreferredPort(ports);
        setPort((prev) => {
          if (ports.some((portInfo) => portInfo.device === prev)) {
            return prev;
          }
          if (prev && prev !== DEFAULT_SERIAL_PORT) {
            return prev;
          }
          return preferredPort;
        });
      }
    } catch (error) {
      setPortsError(error instanceof Error ? error.message : "Failed to list serial ports");
    } finally {
      setPortsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (mode === "serial") {
      void refreshSerialPorts();
    }
  }, [mode, refreshSerialPorts]);

  // Connection card — the first, prominent step: every console action needs an
  // open port. While closed it shows the port chooser; once open it collapses to
  // a compact status strip so the command/console area below is the focus.
  const controls = (
    <div className="card conn-card">
      <div className="card-head">
        <div className="card-titles">
          <div className="card-title">Connection</div>
          <div className="card-sub">
            {isOpen ? "Connected — manage the session below" : "Step 1 — select a serial port, then Open"}
          </div>
        </div>
        {isOpen ? (
          <div className="card-actions">
            <button
              type="button"
              className="btn"
              style={{ color: "var(--danger)", borderColor: "var(--danger)" }}
              onClick={() => void handleClose()}
            >
              Close connection
            </button>
          </div>
        ) : null}
      </div>

      {isOpen ? (
        <div className="conn-status">
          <span className="pill ok">
            <span className="dot" />
            Connected
          </span>
          <span className="conn-meta">
            {mode === "serial" ? port || "serial" : `replay · ${replayPath}`}
            {mode === "serial" && baudrate ? ` · ${baudrate} baud` : ""}
          </span>
        </div>
      ) : (
        <div className="conn-form">
          <div className="conn-modes">
            <button type="button" className="btn" onClick={() => setMode("serial")} disabled={mode === "serial"}>
              Serial
            </button>
            <button type="button" className="btn" onClick={() => setMode("replay")} disabled={mode === "replay"}>
              Replay
            </button>
          </div>

          {mode === "serial" ? (
            <div className="conn-fields">
              <div className="conn-row">
                <select className="conn-port" value={port} onChange={(e) => setPort(e.target.value)}>
                  <option value="">Select detected serial port</option>
                  {serialPorts.map((serialPort) => (
                    <option key={serialPort.device} value={serialPort.device}>
                      {serialPort.description ? `${serialPort.device} (${serialPort.description})` : serialPort.device}
                    </option>
                  ))}
                </select>
                <button type="button" className="btn" onClick={() => void refreshSerialPorts()} disabled={portsLoading}>
                  {portsLoading ? "Refreshing…" : "Refresh"}
                </button>
                <button
                  type="button"
                  className="btn primary"
                  onClick={() => void handleOpen()}
                  disabled={!dutLabel.trim()}
                  title={dutLabel.trim() ? "Open the serial session" : "Enter a DUT label first"}
                >
                  Open
                </button>
              </div>
              <input
                className="conn-input"
                value={dutLabel}
                onChange={(e) => setDutLabel(e.target.value)}
                placeholder="DUT label — names the log, e.g. AP6420E (required)"
                aria-label="DUT label"
                maxLength={40}
              />
              <input
                className="conn-input"
                value={port}
                onChange={(e) => setPort(e.target.value)}
                placeholder="Manual serial port override (optional, e.g. /dev/ttyUSB0)"
              />
              <input
                className="conn-input conn-baud"
                type="number"
                value={baudrate}
                onChange={(e) => setBaudrate(Number(e.target.value || 0))}
                placeholder="Baudrate"
              />
              {portsError ? <div className="conn-error">{portsError}</div> : null}
            </div>
          ) : (
            <div className="conn-row">
              <input
                className="conn-input"
                value={replayPath}
                onChange={(e) => setReplayPath(e.target.value)}
                placeholder="Replay file"
              />
              <input
                className="conn-input"
                type="number"
                value={replayIntervalMs}
                onChange={(e) => setReplayIntervalMs(Number(e.target.value || 0))}
                placeholder="Replay interval ms"
              />
              <button type="button" className="btn primary" onClick={() => void handleOpen()}>
                Open
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );

  const allCriticalCrashLines = useMemo(() => {
    return lines.filter((line) => crashPattern.test(line));
  }, [lines, crashPattern]);

  const newCriticalCrashCount = Math.max(0, allCriticalCrashLines.length - lastSeenCriticalCrashCount);
  const criticalCrashRows = useMemo(() => {
    const rows = allCriticalCrashLines.map((text, index) => ({
      text,
      isNew: index >= lastSeenCriticalCrashCount,
    }));
    return rows.slice(-20);
  }, [allCriticalCrashLines, lastSeenCriticalCrashCount]);

  return (
    <div style={{ fontFamily: "sans-serif", maxWidth: 1100, margin: "0 auto", padding: 16 }}>
      <h1 style={{ textAlign: "center" }}>DUT Dashboard - Milestone 3</h1>
      {controls}
      {actionError ? (
        <div className="conn-banner" role="alert">
          <span className="conn-banner-icon" aria-hidden>⚠</span>
          {actionError}
        </div>
      ) : null}
      <div style={{ border: "1px solid #ddd", padding: 12, marginBottom: 12 }}>
        <div
          style={{
            display: "grid",
            gap: 12,
            gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
            alignItems: "start",
          }}
        >
          <div>
            <h3 style={{ marginTop: 0, marginBottom: 8 }}>CPU Monitor Commands</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-start" }}>
              <button type="button" onClick={() => void handleRunTop()}>
                Memory Info
              </button>
              <button type="button" onClick={() => void runAction(() => handleStopCommand())}>
                Stop
              </button>
            </div>
          </div>
          <div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
              <h3 style={{ marginTop: 0, marginBottom: 0, color: "#b71c1c" }}>
                Critical Crash ({allCriticalCrashLines.length})
              </h3>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span
                  style={{
                    background: newCriticalCrashCount > 0 ? "#b71c1c" : "#9e9e9e",
                    color: "#fff",
                    borderRadius: 999,
                    padding: "2px 8px",
                    fontSize: 11,
                    fontWeight: 700,
                  }}
                >
                  New {newCriticalCrashCount}
                </span>
                <button
                  type="button"
                  onClick={() => setLastSeenCriticalCrashCount(allCriticalCrashLines.length)}
                  disabled={newCriticalCrashCount === 0}
                >
                  Mark as seen
                </button>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "center", flexWrap: "wrap" }}>
              <input
                value={criticalCrashKeywordInput}
                onChange={(e) => setCriticalCrashKeywordInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleLockCriticalCrashKeyword();
                  }
                }}
                placeholder="Lock in critical crash keyword"
                style={{ minWidth: 220, flex: "1 1 220px" }}
              />
              <button type="button" onClick={handleLockCriticalCrashKeyword} disabled={keywordSaving}>
                Lock in
              </button>
            </div>
            {lockedCriticalCrashKeywords.length > 0 ? (
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                {lockedCriticalCrashKeywords.map((keyword) => (
                  <button
                    key={keyword}
                    type="button"
                    onClick={() => handleRemoveCriticalCrashKeyword(keyword)}
                    title="Remove keyword"
                    style={{
                      border: "1px solid #f3b7b7",
                      background: "#fff",
                      borderRadius: 999,
                      padding: "2px 8px",
                      fontSize: 12,
                      color: "#4a1515",
                      cursor: "pointer",
                    }}
                  >
                    {keyword} x
                  </button>
                ))}
              </div>
            ) : null}
            <div
              style={{
                border: "1px solid #f3b7b7",
                background: "#fff6f6",
                color: "#4a1515",
                borderRadius: 6,
                minHeight: 72,
                maxHeight: 140,
                overflowY: "auto",
                padding: 8,
                fontFamily: "monospace",
                fontSize: 12,
                whiteSpace: "pre-wrap",
              }}
            >
              {criticalCrashRows.length > 0 ? (
                criticalCrashRows.map((row, index) => (
                  <div
                    key={`${index}-${row.text}`}
                    style={{
                      background: row.isNew ? "#ffe0e0" : "transparent",
                      padding: row.isNew ? "1px 2px" : 0,
                      borderRadius: 2,
                    }}
                  >
                    {row.text}
                  </div>
                ))
              ) : (
                <div>No critical crash detected yet (kernel panic / Q6 crash / watchdog).</div>
              )}
            </div>
          </div>
        </div>
      </div>
      <div style={{ marginTop: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
          <button type="button" onClick={handleCloseTerminal} disabled={consoleView === "monitor"}>
            Monitor
          </button>
          <button type="button" onClick={() => void handleOpenTerminal()} disabled={consoleView === "terminal"}>
            Terminal (vi / nano)
          </button>
          {consoleView === "terminal" ? (
            <span style={{ fontSize: 12, color: "#8a4b00" }}>Monitoring paused while in terminal mode.</span>
          ) : null}
        </div>
        {consoleView === "terminal" ? (
          <Suspense fallback={<div style={{ padding: 16, color: "#666" }}>Loading terminal…</div>}>
            <TerminalView dutId={dutId} />
          </Suspense>
        ) : (
          <ConsolePanel
            lines={lines}
            linesStartSeq={linesStartSeq}
            onSend={handleSend}
            onDownloadLog={() => void runAction(handleDownloadLog)}
            canDownloadLog={Boolean(currentLogFileName)}
          />
        )}
      </div>
      {downloadNotice ? (
        <div
          style={{
            position: "fixed",
            right: 16,
            bottom: 16,
            background: downloadNotice.tone === "blue" ? "#1565c0" : "#1b5e20",
            color: "#fff",
            padding: "10px 12px",
            borderRadius: 8,
            boxShadow: "0 4px 10px rgba(0, 0, 0, 0.2)",
            fontSize: 13,
            zIndex: 9999,
          }}
        >
          {downloadNotice.message}
        </div>
      ) : null}
    </div>
  );
}
