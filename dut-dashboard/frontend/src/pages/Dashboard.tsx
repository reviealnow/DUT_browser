import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";

import {
  closeSerial,
  enterTerminal,
  exitTerminal,
  getSerialLogDownloadUrl,
  listSerialPorts,
  openSerial,
  sendSerial,
  SerialPortInfo,
} from "../api/rest";
import ConsolePanel from "../components/ConsolePanel";
// Lazy-loaded so the xterm.js bundle only loads when the terminal is opened.
const TerminalView = lazy(() => import("../components/TerminalView"));
import { CRITICAL_CRASH_PATTERN } from "../monitoring/crash";
import { useDutMonitorContext } from "../monitoring/DutMonitorContext";
const DEFAULT_SERIAL_PORT = "/dev/ttyUSB0";

function choosePreferredPort(ports: SerialPortInfo[]): string {
  if (ports.length === 0) {
    return "";
  }
  const macosCuPort = ports.find((portInfo) => portInfo.device.startsWith("/dev/cu."));
  return macosCuPort ? macosCuPort.device : ports[0].device;
}

export default function Dashboard() {
  // Console lines come from the single shared WebSocket (useDutMonitor) instead
  // of Dashboard opening its own connection.
  const { lines } = useDutMonitorContext();
  const [mode, setMode] = useState<"serial" | "replay">("serial");
  const [port, setPort] = useState(DEFAULT_SERIAL_PORT);
  const [baudrate, setBaudrate] = useState(115200);
  const [replayPath, setReplayPath] = useState("logs/sample.log");
  const [replayIntervalMs, setReplayIntervalMs] = useState(100);
  const [serialPorts, setSerialPorts] = useState<SerialPortInfo[]>([]);
  const [portsLoading, setPortsLoading] = useState(false);
  const [portsError, setPortsError] = useState("");
  const [currentLogFileName, setCurrentLogFileName] = useState("");
  const [consoleView, setConsoleView] = useState<"monitor" | "terminal">("monitor");
  const [terminalError, setTerminalError] = useState("");
  const [lastSeenCriticalCrashCount, setLastSeenCriticalCrashCount] = useState(0);
  const [criticalCrashKeywordInput, setCriticalCrashKeywordInput] = useState("");
  const [lockedCriticalCrashKeywords, setLockedCriticalCrashKeywords] = useState<string[]>([]);
  const [downloadNotice, setDownloadNotice] = useState<{ message: string; tone: "blue" | "green" } | null>(null);

  async function handleOpen() {
    const response = await openSerial({
      mode,
      port,
      baudrate,
      replay_path: mode === "replay" ? replayPath : undefined,
      replay_interval_ms: replayIntervalMs,
    });
    const logPath = response.log_path || "";
    const fileName = logPath.split(/[\\/]/).pop() || "";
    setCurrentLogFileName(fileName);
  }

  async function handleClose() {
    await closeSerial();
  }

  async function handleSend(text: string) {
    await sendSerial(text);
  }

  async function handleOpenTerminal() {
    setTerminalError("");
    try {
      await enterTerminal(); // backend switches to raw mode; sysmon monitoring pauses
      setConsoleView("terminal");
    } catch (error) {
      setTerminalError(error instanceof Error ? error.message : "Failed to open terminal");
    }
  }

  function handleCloseTerminal() {
    // Unmount the terminal first (closes /ws/term), then resume monitoring.
    setConsoleView("monitor");
    void exitTerminal();
  }

  // Safety: if this section unmounts (e.g. user switches sidebar nav) while in
  // terminal mode, resume monitoring on the backend. exitTerminal is idempotent.
  useEffect(() => {
    return () => {
      void exitTerminal();
    };
  }, []);

  async function handleRunTop() {
    await sendSerial("top\n");
  }

  async function handleStopCommand() {
    await sendSerial("\u0003");
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
    if (!keyword) {
      return;
    }
    setLockedCriticalCrashKeywords((prev) => {
      if (prev.some((item) => item.toLowerCase() === keyword.toLowerCase())) {
        return prev;
      }
      return [...prev, keyword];
    });
    setCriticalCrashKeywordInput("");
  }

  function handleRemoveCriticalCrashKeyword(keywordToRemove: string) {
    setLockedCriticalCrashKeywords((prev) =>
      prev.filter((item) => item.toLowerCase() !== keywordToRemove.toLowerCase()),
    );
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

  const controls = useMemo(
    () => (
      <div style={{ border: "1px solid #ddd", padding: 12, marginBottom: 12, position: "relative" }}>
        <div style={{ position: "absolute", top: 8, right: 8, display: "flex", gap: 8, alignItems: "center" }}>
          <button
            onClick={handleClose}
            style={{
              width: 28,
              height: 28,
              background: "#d32f2f",
              color: "#fff",
              border: "1px solid #b71c1c",
              borderRadius: 6,
              fontSize: 16,
              fontWeight: 700,
              lineHeight: 1,
              cursor: "pointer",
            }}
            aria-label="Close serial connection"
            title="Close"
          >
            X
          </button>
        </div>
        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <button onClick={() => setMode("serial")} disabled={mode === "serial"}>
            Serial Mode
          </button>
          <button onClick={() => setMode("replay")} disabled={mode === "replay"}>
            Replay Mode
          </button>
        </div>

        {mode === "serial" ? (
          <div style={{ display: "grid", gap: 8 }}>
            <div style={{ display: "flex", gap: 8 }}>
              <select value={port} onChange={(e) => setPort(e.target.value)} style={{ width: 240 }}>
                <option value="">Select detected serial port</option>
                {serialPorts.map((serialPort) => (
                  <option key={serialPort.device} value={serialPort.device}>
                    {serialPort.description ? `${serialPort.device} (${serialPort.description})` : serialPort.device}
                  </option>
                ))}
              </select>
              <button type="button" onClick={() => void refreshSerialPorts()} disabled={portsLoading}>
                {portsLoading ? "Refreshing..." : "Refresh Ports"}
              </button>
              <button
                type="button"
                onClick={() => void handleOpen()}
                style={{
                  background: "#1976d2",
                  color: "#fff",
                  border: "1px solid #1565c0",
                  padding: "6px 12px",
                  fontSize: 14,
                  fontWeight: 600,
                  borderRadius: 6,
                }}
              >
                Open
              </button>
            </div>
            <input
              value={port}
              onChange={(e) => setPort(e.target.value)}
              placeholder="Manual serial port override (optional, e.g. /dev/ttyUSB0)"
              style={{ width: 240 }}
            />
            <input
              type="number"
              value={baudrate}
              onChange={(e) => setBaudrate(Number(e.target.value || 0))}
              placeholder="Baudrate"
              style={{ width: 88 }}
            />
            {portsError ? <div style={{ color: "#b00020", fontSize: 12 }}>{portsError}</div> : null}
          </div>
        ) : (
          <div style={{ display: "flex", gap: 8 }}>
            <input value={replayPath} onChange={(e) => setReplayPath(e.target.value)} placeholder="Replay file" />
            <input
              type="number"
              value={replayIntervalMs}
              onChange={(e) => setReplayIntervalMs(Number(e.target.value || 0))}
              placeholder="Replay interval ms"
            />
            <button
              type="button"
              onClick={() => void handleOpen()}
              style={{
                background: "#1976d2",
                color: "#fff",
                border: "1px solid #1565c0",
                padding: "6px 12px",
                fontSize: 14,
                fontWeight: 600,
                borderRadius: 6,
              }}
            >
              Open
            </button>
          </div>
        )}
      </div>
    ),
    [
      mode,
      port,
      baudrate,
      replayPath,
      replayIntervalMs,
      serialPorts,
      portsLoading,
      portsError,
      refreshSerialPorts,
      currentLogFileName,
    ],
  );

  const allCriticalCrashLines = useMemo(() => {
    return lines.filter((line) => {
      if (CRITICAL_CRASH_PATTERN.test(line)) {
        return true;
      }
      const lowerCasedLine = line.toLowerCase();
      return lockedCriticalCrashKeywords.some((keyword) => lowerCasedLine.includes(keyword.toLowerCase()));
    });
  }, [lines, lockedCriticalCrashKeywords]);

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
              <button type="button" onClick={() => void handleStopCommand()}>
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
              <button type="button" onClick={handleLockCriticalCrashKeyword}>
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
          {terminalError ? <span style={{ fontSize: 12, color: "#b00020" }}>{terminalError}</span> : null}
        </div>
        {consoleView === "terminal" ? (
          <Suspense fallback={<div style={{ padding: 16, color: "#666" }}>Loading terminal…</div>}>
            <TerminalView />
          </Suspense>
        ) : (
          <ConsolePanel
            lines={lines}
            onSend={handleSend}
            onDownloadLog={handleDownloadLog}
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
