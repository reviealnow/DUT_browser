import { Fragment, useState } from "react";

import {
  getWifiClients,
  getWifiClientStats,
  kickWifiClient,
  WifiClientRow,
  WifiClientsResult,
  WifiClientStats,
} from "../api/rest";
import { Card, EmptyState } from "./shell/Card";

const COL_COUNT = 11;

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
export default function WifiClientsCard() {
  const [data, setData] = useState<WifiClientsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [statsByMac, setStatsByMac] = useState<Record<string, StatsCell>>({});

  async function scan() {
    setLoading(true);
    setError("");
    try {
      setData(await getWifiClients());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Scan failed");
    } finally {
      setLoading(false);
    }
  }

  async function kick(client: WifiClientRow) {
    if (!window.confirm(`Disassociate ${client.mac} from ${client.iface}?`)) {
      return;
    }
    try {
      await kickWifiClient(client.iface, client.mac);
      await scan();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Kick failed");
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
          getWifiClientStats(mac)
            .then((r) => setStatsByMac((s) => ({ ...s, [mac]: { loading: false, stats: r.stats } })))
            .catch((e) =>
              setStatsByMac((s) => ({ ...s, [mac]: { loading: false, error: e instanceof Error ? e.message : "Failed" } })),
            );
        }
      }
      return next;
    });
  }

  const action = (
    <button className="btn" onClick={scan} disabled={loading}>
      {loading ? "Scanning…" : "Scan clients"}
    </button>
  );

  return (
    <Card title="Wi-Fi clients (detail)" subtitle="Per-client RF detail via wlanconfig" actions={action}>
      {error ? <div className="flash" style={{ marginBottom: "var(--space-3)" }}>{error}</div> : null}
      {data === null ? (
        <EmptyState icon="📶" message="No scan yet" hint="Open a DUT serial connection, then Scan clients." />
      ) : data.clients.length === 0 ? (
        <EmptyState icon="📶" message="No associated clients" hint={`Scanned ${data.vaps.length} VAP(s) at ${data.captured_at}.`} />
      ) : (
        <div style={{ overflowX: "auto" }}>
        <table className="filetable">
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
            {data.clients.map((c) => (
              <Fragment key={`${c.iface}-${c.mac}`}>
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
