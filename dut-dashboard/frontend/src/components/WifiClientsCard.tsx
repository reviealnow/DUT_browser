import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import {
  getWifiClientStats,
  humanizeApiError,
  kickWifiClient,
  WifiClientRow,
  WifiClientStats,
} from "../api/rest";
import { DEFAULT_DUT_ID } from "../api/dut";
import { useWifiScan, wifiScanForDut } from "../monitoring/WifiScanContext";
import { Card, EmptyState } from "./shell/Card";

const COL_COUNT = 11;
const BAND_ORDER: Record<string, number> = { "2.4G": 0, "5G": 1, "6G": 2 };

function bandRank(band: string): number {
  return band in BAND_ORDER ? BAND_ORDER[band] : 99;
}

function formatBytes(n: number | null): string {
  if (n === null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** kbps → "x.x Mbps" (avg rates are already in kbps; 1s byte counts → *8/1000 kbps). */
function mbps(kbps: number | null): string {
  return kbps === null ? "—" : `${(kbps / 1000).toFixed(1)} Mbps`;
}

type StatsCell = { loading: boolean; error?: string; stats?: WifiClientStats };

/**
 * On-demand Wi-Fi client detail. "Scan clients" runs `wlanconfig <vap> list`
 * across active VAPs (light RF table). Expanding a row lazily fetches per-client
 * deep stats via `apstats -s -m <MAC>` (one serial command per client) — Tx/Rx
 * bytes, throughput, channel width, NSS, Rx RSSI, PER — cached per MAC.
 */
export default function WifiClientsCard({ dutId = DEFAULT_DUT_ID }: { dutId?: string }) {
  // Scan result/loading/error come from the shared cache so the Wi-Fi section
  // and the Overview reuse one RPC instead of each firing their own.
  const wifi = useWifiScan();
  const { result: data, error: scanError, loading } = wifiScanForDut(wifi, dutId);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [statsByMac, setStatsByMac] = useState<Record<string, StatsCell>>({});

  const scan = useCallback(() => wifi.scan(dutId), [wifi, dutId]);

  // Auto-scan when the section opens or the selected DUT changes — but reuse an
  // existing SUCCESSFUL scan for this DUT instead of re-firing the serial RPC
  // (e.g. one already run from the Overview). A cached error still retries on
  // entry; a scan in flight is left alone. Manual "Scan clients" always refreshes.
  useEffect(() => {
    if (wifi.dutId !== dutId || (wifi.result === null && !wifi.loading)) {
      void wifi.scan(dutId);
    }
    // Intentionally keyed on dutId only: the effect runs once per mount / DUT
    // switch (not on every cache change, which would loop). The guard reads the
    // latest cache via the captured `wifi`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dutId]);

  async function kick(client: WifiClientRow) {
    if (!window.confirm(`Disassociate ${client.mac} from ${client.iface}?`)) {
      return;
    }
    try {
      await kickWifiClient(client.iface, client.mac, dutId);
      await scan();
    } catch (e) {
      setError(humanizeApiError(e));
    }
  }

  function toggle(mac: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(mac)) {
        next.delete(mac);
      } else {
        next.add(mac);
        // Lazy fetch once per MAC (cached across collapse/expand).
        if (!statsByMac[mac]) {
          setStatsByMac((s) => ({ ...s, [mac]: { loading: true } }));
          getWifiClientStats(mac, dutId)
            .then((r) => setStatsByMac((s) => ({ ...s, [mac]: { loading: false, stats: r.stats } })))
            .catch((e) =>
              setStatsByMac((s) => ({ ...s, [mac]: { loading: false, error: e instanceof Error ? e.message : "Failed" } })),
            );
        }
      }
      return next;
    });
  }

  // Sort by band (2.4→5→6) then strongest signal; per-band counts for the summary.
  const sortedClients = useMemo(() => {
    const cs = data?.clients ?? [];
    return [...cs].sort(
      (a, b) => bandRank(a.band) - bandRank(b.band) || (b.signal_pct ?? -999) - (a.signal_pct ?? -999),
    );
  }, [data]);

  const bandCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of data?.clients ?? []) m[c.band] = (m[c.band] ?? 0) + 1;
    return m;
  }, [data]);

  const action = (
    <button className="btn" onClick={scan} disabled={loading}>
      {loading ? "Scanning…" : "Scan clients"}
    </button>
  );

  return (
    <Card title="Wi-Fi clients (detail)" subtitle="Per-client RF detail via wlanconfig" actions={action}>
      {error || scanError ? (
        <div className="flash" style={{ marginBottom: "var(--space-3)" }}>{error || scanError}</div>
      ) : null}
      {data === null ? (
        <EmptyState icon="📶" message="No scan yet" hint="Open a DUT serial connection, then Scan clients." />
      ) : data.clients.length === 0 ? (
        <EmptyState icon="📶" message="No associated clients" hint={`Scanned ${data.vaps.length} VAP(s) at ${data.captured_at}.`} />
      ) : (
        <>
        <div className="wifi-summary" style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", marginBottom: "var(--space-3)" }}>
          <span className="pill">{data.clients.length} clients</span>
          {["2.4G", "5G", "6G"].filter((b) => bandCounts[b]).map((b) => (
            <span key={b} className="pill" style={{ background: "var(--accent-weak)", color: "var(--accent)" }}>
              {b}: {bandCounts[b]}
            </span>
          ))}
          <span style={{ color: "var(--faint)", fontSize: 12, alignSelf: "center" }}>scanned {data.captured_at}</span>
        </div>
        <div style={{ overflowX: "auto" }}>
        <table className="filetable wifitable">
          <thead>
            <tr>
              <th>MAC</th>
              <th>SSID / Band</th>
              <th>Chan</th>
              <th>Signal</th>
              <th>SNR</th>
              <th>TX / RX</th>
              <th>Mode</th>
              <th>NSS</th>
              <th>Assoc</th>
              <th>Vendor</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {sortedClients.map((c, i) => (
              <Fragment key={`${c.iface}-${c.mac}`}>
                {i === 0 || sortedClients[i - 1].band !== c.band ? (
                  <tr className="wifi-band-row">
                    <td colSpan={COL_COUNT}>{c.band} · {bandCounts[c.band]} client{bandCounts[c.band] === 1 ? "" : "s"}</td>
                  </tr>
                ) : null}
                <tr>
                  <td className="filetable-name">
                    <button
                      className="btn"
                      onClick={() => toggle(c.mac)}
                      title="Show per-client stats"
                      style={{ padding: "0 6px", marginRight: 6 }}
                    >
                      {expanded.has(c.mac) ? "▾" : "▸"}
                    </button>
                    {c.mac}
                  </td>
                  <td>{c.ssid ?? "—"} · {c.band}</td>
                  <td>{c.channel ?? "—"}</td>
                  <td>{c.signal_pct === null ? "—" : `${c.signal_pct}%`}{c.rssi === null ? "" : ` (${c.rssi})`}</td>
                  <td>{c.snr ?? "—"}</td>
                  <td>{c.txrate ?? "—"} / {c.rxrate ?? "—"}</td>
                  <td>{c.phymode ?? "—"}{c.width ? ` · ${c.width}` : ""}</td>
                  <td>{c.rxnss === null ? "—" : `${c.rxnss}×${c.txnss}`}</td>
                  <td>{c.assoc_time ?? "—"}</td>
                  <td>{c.vendor || "—"}</td>
                  <td>
                    <button
                      className="btn"
                      onClick={() => kick(c)}
                      style={{ padding: "2px 10px", color: "var(--danger)", borderColor: "var(--danger)" }}
                    >
                      Kick
                    </button>
                  </td>
                </tr>
                {expanded.has(c.mac) ? (
                  <tr>
                    <td colSpan={COL_COUNT} style={{ background: "var(--panel-2, #f7f8fa)" }}>
                      <ClientStatsDetail cell={statsByMac[c.mac]} />
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            ))}
          </tbody>
        </table>
        </div>
        </>
      )}
      {data && data.clients.length > 0 ? (
        <script type="application/json" id="wifi-clients-data" dangerouslySetInnerHTML={{ __html: JSON.stringify(data.clients) }} />
      ) : null}
    </Card>
  );
}

function ClientStatsDetail({ cell }: { cell?: StatsCell }) {
  if (!cell || cell.loading) {
    return <span style={{ color: "var(--faint)" }}>Loading stats…</span>;
  }
  if (cell.error) {
    return <span style={{ color: "var(--danger)" }}>{cell.error}</span>;
  }
  const s = cell.stats!;
  const items: [string, string][] = [
    ["Tx bytes", formatBytes(s.tx_bytes)],
    ["Rx bytes", formatBytes(s.rx_bytes)],
    ["Avg Tx", mbps(s.avg_tx_kbps)],
    ["Avg Rx", mbps(s.avg_rx_kbps)],
    ["Tx now", s.tx_bytes_1s === null ? "—" : `${formatBytes(s.tx_bytes_1s)}/s`],
    ["Rx now", s.rx_bytes_1s === null ? "—" : `${formatBytes(s.rx_bytes_1s)}/s`],
    ["Width", s.band_width === null ? "—" : `${s.band_width} MHz`],
    ["NSS", s.tx_nss === null ? "—" : `${s.rx_nss}×${s.tx_nss}`],
    ["Rx RSSI", s.rx_rssi === null ? "—" : `${s.rx_rssi}`],
    ["PER", s.per === null ? "—" : `${s.per}`],
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: "var(--space-2)" }}>
      {items.map(([label, value]) => (
        <div key={label}>
          <div style={{ fontSize: 11, color: "var(--faint)", textTransform: "uppercase" }}>{label}</div>
          <div style={{ fontWeight: 600 }}>{value}</div>
        </div>
      ))}
    </div>
  );
}
