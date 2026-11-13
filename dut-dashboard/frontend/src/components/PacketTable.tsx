import { useEffect, useMemo, useRef, useState } from "react";

import { humanizeApiError } from "../api/rest";
import type { PacketColumn, PacketRowsPage, PacketSeverity } from "../api/rest";

/**
 * The decoded rows of one analysis, rendered as a window (§B11).
 *
 * An analysis can hold hundreds of thousands of rows; only the ones in view,
 * plus a few either side, are in the DOM. Rows arrive a page at a time from
 * `page`, which the context caches, and a row not loaded yet draws as a
 * placeholder of the same height so the scrollbar never jumps.
 *
 * Only the pages within KEEP_PAGES of the window are kept; scrolling away
 * drops the rest, and scrolling back asks `page` again, whose own cache
 * answers a page still in flight with the same request.
 */
export const ROW_HEIGHT = 28;
export const VIEW_HEIGHT = 480;
export const PAGE_SIZE = 200;
const OVERSCAN = 10;
export const KEEP_PAGES = 2;

type Loaded = { row: Record<string, unknown>; display: Record<string, string> };

type PacketTableProps = {
  total: number;
  columns: PacketColumn[];
  page: (offset: number, limit: number) => Promise<PacketRowsPage>;
  /** The worst finding each frame is part of, for the row's colour. */
  severityByFrame: Map<number, PacketSeverity>;
  selectedFrame: number | null;
  onOpen: (frame: number) => void;
};

export function PacketTable({ total, columns, page, severityByFrame, selectedFrame, onOpen }: PacketTableProps) {
  const [scrollTop, setScrollTop] = useState(0);
  // Rows by page number, for the pages near the window only.
  const [loaded, setLoaded] = useState<Map<number, Loaded[]>>(new Map());
  const [error, setError] = useState("");
  // Pages asked for and still kept, per `page` (one analysis): each is asked
  // for once while it stays near the window.
  const requested = useRef<{ source: typeof page; pages: Set<number> }>({ source: page, pages: new Set() });

  // A new analysis (a new `page`) starts from an empty window.
  useEffect(() => {
    if (requested.current.source !== page) {
      requested.current = { source: page, pages: new Set() };
      setLoaded(new Map());
      setScrollTop(0);
      setError("");
    }
  }, [page]);

  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const last = Math.min(total, Math.ceil((scrollTop + VIEW_HEIGHT) / ROW_HEIGHT) + OVERSCAN);

  useEffect(() => {
    if (total === 0) {
      return;
    }
    const state = requested.current;
    if (state.source !== page) {
      return; // the reset above runs first and brings us back here
    }
    const firstPage = Math.floor(first / PAGE_SIZE);
    const lastPage = Math.floor(Math.max(first, last - 1) / PAGE_SIZE);
    const near = (number: number) => number >= firstPage - KEEP_PAGES && number <= lastPage + KEEP_PAGES;
    // Forget what the window has left behind: rows, and that they were asked for.
    for (const number of [...state.pages]) {
      if (!near(number)) state.pages.delete(number);
    }
    setLoaded((previous) => {
      const stale = [...previous.keys()].filter((number) => !near(number));
      if (!stale.length) return previous;
      const next = new Map(previous);
      stale.forEach((number) => next.delete(number));
      return next;
    });
    for (let number = firstPage; number <= lastPage; number += 1) {
      if (state.pages.has(number)) {
        continue;
      }
      state.pages.add(number);
      page(number * PAGE_SIZE, PAGE_SIZE)
        .then((result) => {
          if (requested.current !== state || !state.pages.has(number)) {
            return; // an analysis no longer shown, or a page the window has left
          }
          setLoaded((previous) => {
            const next = new Map(previous);
            next.set(
              number,
              result.rows.map((row, i) => ({ row, display: result.display[i] ?? {} })),
            );
            return next;
          });
        })
        .catch((e: unknown) => {
          if (requested.current === state) {
            state.pages.delete(number);
            setError(humanizeApiError(e));
          }
        });
    }
  }, [first, last, total, page]);

  const indexes = useMemo(() => {
    const out: number[] = [];
    for (let index = first; index < last; index += 1) {
      out.push(index);
    }
    return out;
  }, [first, last]);

  if (total === 0) {
    return <div className="pkt-empty">No frames matched.</div>;
  }

  return (
    <>
      {error ? <div className="pkt-error">{error}</div> : null}
      <div
        className="pkt-window"
        style={{ height: VIEW_HEIGHT }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
        data-testid="packet-window"
      >
        <table className="filetable pkt-table">
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.key}>{column.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr aria-hidden style={{ height: first * ROW_HEIGHT }} />
            {indexes.map((index) => {
              const item = loaded.get(Math.floor(index / PAGE_SIZE))?.[index % PAGE_SIZE];
              if (!item) {
                return (
                  <tr key={index} className="pkt-row pkt-pending" style={{ height: ROW_HEIGHT }}>
                    <td colSpan={columns.length}>…</td>
                  </tr>
                );
              }
              const frame = typeof item.row.frame === "number" ? item.row.frame : null;
              const severity = frame !== null ? severityByFrame.get(frame) : undefined;
              const classes = ["pkt-row"];
              if (severity) classes.push(`pkt-${severity}`);
              if (frame !== null && frame === selectedFrame) classes.push("pkt-selected");
              return (
                <tr
                  key={index}
                  className={classes.join(" ")}
                  style={{ height: ROW_HEIGHT }}
                  onClick={() => frame !== null && onOpen(frame)}
                  aria-selected={frame !== null && frame === selectedFrame}
                >
                  {columns.map((column) => (
                    <td key={column.key} title={rawTitle(item.row[column.key])}>
                      {column.key in item.display ? item.display[column.key] : ""}
                    </td>
                  ))}
                </tr>
              );
            })}
            <tr aria-hidden style={{ height: (total - last) * ROW_HEIGHT }} />
          </tbody>
        </table>
      </div>
    </>
  );
}

/** The raw decoded value, shown on hover (§B6). */
function rawTitle(value: unknown): string {
  if (value === undefined) return "not resolved by this Host's tshark";
  if (value === null) return "absent in this frame";
  return typeof value === "string" ? value : JSON.stringify(value);
}
