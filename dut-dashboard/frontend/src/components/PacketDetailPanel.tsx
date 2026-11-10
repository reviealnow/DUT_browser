import type { ReactNode } from "react";

/**
 * One frame's dissection tree, as tshark's `-T json` prints it (§B9, §B11).
 * A side panel rather than a popup, so the table stays in view beside it.
 */
type PacketDetailPanelProps = {
  frame: number;
  tree: unknown;
  loading: boolean;
  error: string;
  onClose: () => void;
};

export function PacketDetailPanel({ frame, tree, loading, error, onClose }: PacketDetailPanelProps) {
  const layers = layersOf(tree);
  return (
    <aside className="pkt-detail" aria-label={`Frame ${frame} detail`}>
      <div className="pkt-detail-head">
        <strong>Frame {frame}</strong>
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
      {loading ? <div className="pkt-muted">Asking the Host…</div> : null}
      {error ? <div className="pkt-error">{error}</div> : null}
      {layers ? (
        <div className="pkt-tree">
          {Object.entries(layers).map(([name, value]) => (
            <Node key={name} name={name} value={value} open={name !== "frame"} />
          ))}
        </div>
      ) : null}
    </aside>
  );
}

function layersOf(tree: unknown): Record<string, unknown> | null {
  const source = (tree as { _source?: { layers?: unknown } } | null)?._source?.layers;
  return source && typeof source === "object" ? (source as Record<string, unknown>) : null;
}

function Node({ name, value, open = false }: { name: string; value: unknown; open?: boolean }): ReactNode {
  if (value !== null && typeof value === "object") {
    const entries = Array.isArray(value)
      ? value.map((item, i) => [`${name}[${i}]`, item] as const)
      : Object.entries(value as Record<string, unknown>);
    return (
      <details open={open}>
        <summary>{name}</summary>
        <div className="pkt-tree-children">
          {entries.map(([key, child]) => (
            <Node key={key} name={key} value={child} />
          ))}
        </div>
      </details>
    );
  }
  return (
    <div className="pkt-leaf">
      <span className="pkt-leaf-key">{name}</span>: <span className="pkt-leaf-value">{String(value)}</span>
    </div>
  );
}
