import { useCallback, useState } from "react";

import { CapabilityReport, CapabilityRow, getCapabilityReport, humanizeApiError } from "../api/rest";
import { DEFAULT_DUT_ID } from "../api/dut";
import { Card, EmptyState } from "./shell/Card";

/** Render a boolean capability as a short pill string. */
function fmtBool(v: boolean | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v ? "✓" : "✗";
}

function fmtStr(v: string | null | undefined): string {
  return v ?? "—";
}

/** A compact row showing config vs observed for one SSID. */
function CapabilityRowView({ row }: { row: CapabilityRow }) {
  const [expanded, setExpanded] = useState(false);
  const hasDiffs = row.diffs.length > 0;
  const rowClass = hasDiffs ? "cap-row cap-row--diff" : row.match ? "cap-row cap-row--ok" : "cap-row cap-row--miss";

  return (
    <>
      <tr
        className={rowClass}
        onClick={() => setExpanded((v) => !v)}
        style={{ cursor: "pointer" }}
        aria-expanded={expanded}
      >
        <td className="cap-iface">{row.iface}</td>
        <td className="cap-ssid">{row.ssid ?? <em>hidden</em>}</td>
        <td>{row.band ?? "—"}</td>
        <td>{row.channel ?? "—"}</td>
        <td>{fmtStr(row.config_security)}</td>
        <td>{fmtStr(row.config_pmf)}</td>
        <td>{fmtStr(row.config_generation)}</td>
        <td>{fmtBool(row.config_dot11k)}</td>
        <td>{fmtBool(row.config_dot11v)}</td>
        <td>{fmtBool(row.config_dot11r)}</td>
        <td>
          {row.match ? (
            hasDiffs ? (
              <span className="pill warn">⚠ {row.diffs.length} diff{row.diffs.length > 1 ? "s" : ""}</span>
            ) : (
              <span className="pill ok">✓ match</span>
            )
          ) : row.caveat ? (
            <span className="pill idle" title={row.caveat}>? unscan</span>
          ) : (
            <span className="pill danger">✗ miss</span>
          )}
        </td>
      </tr>
      {expanded ? (
        <tr className="cap-expand">
          <td colSpan={11}>
            <div className="cap-detail">
              <dl className="stat-list">
                <div className="stat-row"><dt>BSSID</dt><dd>{row.bssid ?? "—"}</dd></div>
                <div className="stat-row"><dt>Freq</dt><dd>{row.freq_mhz ? `${row.freq_mhz} MHz` : "—"}</dd></div>
                <div className="stat-row"><dt>Channel width</dt><dd>{row.channel_width ?? "—"}</dd></div>
                {row.match ? (
                  <>
                    <div className="stat-row"><dt>Observed generation</dt><dd>{fmtStr(row.observed_generation)}</dd></div>
                    <div className="stat-row"><dt>Observed security</dt><dd>{fmtStr(row.observed_security)}</dd></div>
                    <div className="stat-row"><dt>Observed PMF</dt><dd>{fmtStr(row.observed_pmf)}</dd></div>
                    <div className="stat-row"><dt>Observed 802.11k/v/r</dt>
                      <dd>{fmtBool(row.observed_dot11k)} / {fmtBool(row.observed_dot11v)} / {fmtBool(row.observed_dot11r)}</dd>
                    </div>
                    {row.observed_signal_dbm !== null && row.observed_signal_dbm !== undefined ? (
                      <div className="stat-row"><dt>Signal (host)</dt><dd>{row.observed_signal_dbm} dBm</dd></div>
                    ) : null}
                  </>
                ) : null}
                {row.caveat ? (
                  <div className="stat-row"><dt>Note</dt><dd style={{ color: "var(--warn)" }}>{row.caveat}</dd></div>
                ) : null}
              </dl>
              {row.diffs.length > 0 ? (
                <div className="cap-diffs">
                  <strong>Diffs (config vs observed):</strong>
                  <table className="diff-table">
                    <thead>
                      <tr><th>Field</th><th>Config (DUT)</th><th>Observed (scan)</th></tr>
                    </thead>
                    <tbody>
                      {row.diffs.map((d) => (
                        <tr key={d.field}>
                          <td>{d.label}</td>
                          <td className="diff-config">{String(d.config ?? "—")}</td>
                          <td className="diff-observed">{String(d.observed ?? "—")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

/** On-demand SSID capability report: DUT config (serial) vs host-side iw scan. */
export default function SsidCapabilityCard({ dutId = DEFAULT_DUT_ID }: { dutId?: string }) {
  const [report, setReport] = useState<CapabilityReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const fetchReport = useCallback(() => {
    setLoading(true);
    setError("");
    getCapabilityReport(dutId)
      .then((r) => { setReport(r); setLoading(false); })
      .catch((e) => { setError(humanizeApiError(e)); setLoading(false); });
  }, [dutId]);

  return (
    <div className="grid">
      <Card
        title="SSID Capability — Config vs Air"
        subtitle="DUT hostapd config (Source A, serial) reconciled with host-side iw scan (Source B)"
        actions={
          <button type="button" className="btn primary" onClick={fetchReport} disabled={loading}>
            {loading ? "Loading…" : report ? "Re-run" : "Run"}
          </button>
        }
      >
        {error ? (
          <EmptyState icon="⚠" message="Fetch failed" hint={error} />
        ) : !report ? (
          <EmptyState
            icon="📡"
            message="Click Run to reconcile"
            hint="Requires an open serial connection to the DUT. Source B (iw scan) needs SURVEY_WIFI_IFACE set on the host."
          />
        ) : (
          <CapabilityReportBody report={report} />
        )}
      </Card>
    </div>
  );
}

function CapabilityReportBody({ report }: { report: CapabilityReport }) {
  const matchCount = report.rows.filter((r) => r.match && r.diffs.length === 0).length;
  const diffCount = report.rows.filter((r) => r.match && r.diffs.length > 0).length;
  const missCount = report.rows.filter((r) => !r.match).length;

  return (
    <div className="cap-body">
      <div className="cap-summary">
        <span className="pill ok">✓ {matchCount} match</span>
        {diffCount > 0 ? <span className="pill warn">⚠ {diffCount} diff</span> : null}
        {missCount > 0 ? <span className="pill danger">✗ {missCount} miss</span> : null}
        {!report.available_b ? (
          <span className="pill idle" title="Set SURVEY_WIFI_IFACE on the host to enable Source B">
            Source B unavailable
          </span>
        ) : report.scannable_bands.length > 0 ? (
          <span className="pill idle">Scanned: {report.scannable_bands.join(" / ")}</span>
        ) : null}
        <span className="cap-ts">A: {report.captured_at_a}{report.captured_at_b ? ` · B: ${report.captured_at_b}` : ""}</span>
      </div>

      <div className="table-wrap" style={{ overflowX: "auto" }}>
        <table className="cap-table">
          <thead>
            <tr>
              <th>Iface</th>
              <th>SSID</th>
              <th>Band</th>
              <th>Ch</th>
              <th>Security</th>
              <th>PMF</th>
              <th>Gen</th>
              <th>k</th>
              <th>v</th>
              <th>r</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {report.rows.map((row) => (
              <CapabilityRowView key={row.iface} row={row} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
