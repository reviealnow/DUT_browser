import { useCallback, useEffect, useRef, useState } from "react";
import {
  getSnapshotDownloadUrl,
  listSnapshots,
  SnapshotFileInfo,
  startSnapshotReplay,
  stopSnapshotReplay,
} from "../api/rest";

type ReplayStatus = "idle" | "playing" | "done" | "stopped";

type Props = {
  replayStatus: ReplayStatus;
  replayProgress: { frame: number; total: number } | null;
  onReplayStatusChange: (status: ReplayStatus) => void;
};

export default function SnapshotReplayPanel({ replayStatus, replayProgress, onReplayStatusChange }: Props) {
  const [files, setFiles] = useState<SnapshotFileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  const [speedMs, setSpeedMs] = useState(500);
  const isMounted = useRef(true);

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  const refreshFiles = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await listSnapshots();
      if (!isMounted.current) return;
      setFiles(result);
      if (result.length > 0 && !selectedFile) {
        setSelectedFile(result[0].name);
      }
    } catch (err) {
      if (isMounted.current) {
        setError(err instanceof Error ? err.message : "Failed to list snapshots");
      }
    } finally {
      if (isMounted.current) setLoading(false);
    }
  }, [selectedFile]);

  useEffect(() => {
    void refreshFiles();
  }, []);

  async function handleStart() {
    if (!selectedFile) return;
    try {
      setError("");
      onReplayStatusChange("playing");
      await startSnapshotReplay(selectedFile, speedMs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start replay");
      onReplayStatusChange("idle");
    }
  }

  async function handleStop() {
    try {
      await stopSnapshotReplay();
    } catch {
      // ignore
    }
  }

  const progressPct =
    replayProgress && replayProgress.total > 0
      ? Math.round((replayProgress.frame / replayProgress.total) * 100)
      : 0;

  const statusLabel: Record<ReplayStatus, string> = {
    idle: "",
    playing: "Playing...",
    done: "Done",
    stopped: "Stopped",
  };

  function formatSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function formatMtime(mtime: number): string {
    return new Date(mtime * 1000).toLocaleString();
  }

  return (
    <div style={{ border: "1px solid #ddd", padding: 12, marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>Snapshot Replay</h3>
        <button type="button" onClick={() => void refreshFiles()} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {error ? (
        <div style={{ color: "#b00020", fontSize: 12, marginBottom: 8 }}>{error}</div>
      ) : null}

      {files.length === 0 && !loading ? (
        <div style={{ color: "#888", fontSize: 13, marginBottom: 8 }}>
          No .jsonl snapshot files found. Start a serial session to record snapshots.
        </div>
      ) : (
        <div
          style={{
            border: "1px solid #e0e0e0",
            borderRadius: 6,
            marginBottom: 10,
            maxHeight: 180,
            overflowY: "auto",
          }}
        >
          {files.map((f) => (
            <div
              key={`${f.mtime}-${f.name}`}
              onClick={() => setSelectedFile(f.name)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "6px 10px",
                cursor: "pointer",
                background: selectedFile === f.name ? "#e3f0ff" : "transparent",
                borderBottom: "1px solid #f0f0f0",
              }}
            >
              <input
                type="radio"
                name="snapshot-file"
                checked={selectedFile === f.name}
                onChange={() => setSelectedFile(f.name)}
                style={{ cursor: "pointer" }}
              />
              <span style={{ flex: 1, fontFamily: "monospace", fontSize: 13 }}>{f.name}</span>
              <span style={{ fontSize: 12, color: "#666", whiteSpace: "nowrap" }}>
                {f.frames} frames · {formatSize(f.size_bytes)}
              </span>
              <span style={{ fontSize: 11, color: "#999", whiteSpace: "nowrap" }}>{formatMtime(f.mtime)}</span>
              <a
                href={getSnapshotDownloadUrl(f.name)}
                download={f.name}
                onClick={(e) => e.stopPropagation()}
                title="Download JSONL"
                style={{
                  fontSize: 14,
                  color: "#1565c0",
                  textDecoration: "none",
                  padding: "2px 4px",
                  borderRadius: 4,
                }}
              >
                ↓
              </a>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
        <label style={{ fontSize: 13, whiteSpace: "nowrap" }}>Speed (ms/frame):</label>
        <input
          type="number"
          value={speedMs}
          min={50}
          max={5000}
          step={50}
          onChange={(e) => setSpeedMs(Math.max(50, Number(e.target.value) || 500))}
          style={{ width: 80 }}
        />
        <button
          type="button"
          onClick={() => void handleStart()}
          disabled={!selectedFile || replayStatus === "playing"}
          style={{
            background: "#1976d2",
            color: "#fff",
            border: "1px solid #1565c0",
            padding: "6px 14px",
            fontSize: 14,
            fontWeight: 600,
            borderRadius: 6,
            cursor: !selectedFile || replayStatus === "playing" ? "not-allowed" : "pointer",
            opacity: !selectedFile || replayStatus === "playing" ? 0.6 : 1,
          }}
        >
          ▶ Start Replay
        </button>
        <button
          type="button"
          onClick={() => void handleStop()}
          disabled={replayStatus !== "playing"}
          style={{
            background: "#d32f2f",
            color: "#fff",
            border: "1px solid #b71c1c",
            padding: "6px 14px",
            fontSize: 14,
            fontWeight: 600,
            borderRadius: 6,
            cursor: replayStatus !== "playing" ? "not-allowed" : "pointer",
            opacity: replayStatus !== "playing" ? 0.5 : 1,
          }}
        >
          ■ Stop
        </button>
        {statusLabel[replayStatus] ? (
          <span
            style={{
              fontSize: 13,
              color:
                replayStatus === "playing"
                  ? "#1565c0"
                  : replayStatus === "done"
                    ? "#2e7d32"
                    : "#795548",
              fontWeight: 500,
            }}
          >
            {statusLabel[replayStatus]}
          </span>
        ) : null}
      </div>

      {replayProgress && replayProgress.total > 0 ? (
        <div>
          <div
            style={{
              background: "#e0e0e0",
              borderRadius: 6,
              height: 10,
              overflow: "hidden",
              marginBottom: 4,
            }}
          >
            <div
              style={{
                width: `${progressPct}%`,
                height: "100%",
                background: replayStatus === "done" ? "#2e7d32" : "#1976d2",
                borderRadius: 6,
                transition: "width 0.2s ease",
              }}
            />
          </div>
          <div style={{ fontSize: 12, color: "#555" }}>
            Frame {replayProgress.frame} / {replayProgress.total} ({progressPct}%)
          </div>
        </div>
      ) : null}
    </div>
  );
}
