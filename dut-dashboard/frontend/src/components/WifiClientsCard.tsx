import { useState } from "react";

import { getWifiClients, kickWifiClient, WifiClientRow, WifiClientsResult } from "../api/rest";
import { Card, EmptyState } from "./shell/Card";

/**
 * On-demand Wi-Fi client detail. Clicking "Scan clients" runs
 * `wlanconfig <vap> list` across the active VAPs on the DUT (driver-level RF
 * detail: RSSI, SNR, rates, NSS, PHY mode/width, assoc time) and renders one row
 * per associated client. Source = the serial console; sysmon monitoring pauses
 * for ~1-2s during the scan. Requires an open serial connection.
 */
export default function WifiClientsCard() {
  const [data, setData] = useState<WifiClientsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

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
              <tr key={`${c.iface}-${c.mac}`}>
                <td className="filetable-name">{c.mac}</td>
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
