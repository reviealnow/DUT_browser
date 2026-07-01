import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ChannelRecommendation,
  ChannelRecommendationResult,
  humanizeApiError,
  ObservedNeighbor,
  getChannelRecommendation,
} from "../api/rest";
import { DEFAULT_DUT_ID } from "../api/dut";
import { Card, EmptyState } from "./shell/Card";

const BAND_ORDER: Record<string, number> = { "2.4GHz": 0, "5GHz": 1, "6GHz": 2 };

function bandRank(band: string | null): number {
  return band !== null && band in BAND_ORDER ? BAND_ORDER[band] : 99;
}

/**
 * DUT-side site survey + per-band channel recommendation. Auto-runs on
 * section entry / DUT switch, mirrors WifiClientsCard's idiom (Card + Scan
 * action + summary pills + filetable). Off-channel scans on all active VAPs
 * are slow (tens of seconds on a busy AP) — the Scan button disables and
 * labels itself while in flight rather than pretending this is instant.
 */
export default function SiteSurveyCard({ dutId = DEFAULT_DUT_ID }: { dutId?: string }) {
  const [data, setData] = useState<ChannelRecommendationResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const scan = useCallback((forDut: string) => {
    setLoading(true);
    setError("");
    getChannelRecommendation(forDut)
      .then((r) => { setData(r); setLoading(false); })
      .catch((e) => { setError(humanizeApiError(e)); setLoading(false); });
  }, []);

  const rerun = useCallback(() => scan(dutId), [scan, dutId]);

  // Auto-run once per DUT (mount or switch); ref-gate survives StrictMode's
  // double-invoke. Manual "Re-scan" always re-fetches.
  const lastDutRef = useRef<string | null>(null);
  useEffect(() => {
    if (lastDutRef.current !== dutId) {
      lastDutRef.current = dutId;
      setData(null);
      scan(dutId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dutId]);

  const sortedNeighbors = useMemo(() => {
    const ns = data?.neighbors ?? [];
    return [...ns].sort(
      (a, b) => bandRank(a.band) - bandRank(b.band) || (b.signal_dbm ?? -999) - (a.signal_dbm ?? -999),
    );
  }, [data]);

  const neighborCountByBand = useMemo(() => {
    const m: Record<string, number> = {};
    for (const n of data?.neighbors ?? []) {
      if (n.band) m[n.band] = (m[n.band] ?? 0) + 1;
    }
    return m;
  }, [data]);

  const action = (
    <button className="btn primary" onClick={rerun} disabled={loading}>
      {loading ? "Scanning…" : data ? "Re-scan" : "Scan"}
    </button>
  );

  return (
    <Card
      title="Site Survey — Channel Recommendation"
      subtitle="DUT-side neighbor scan (iw scan) per band, reconciled with current channel"
      actions={action}
    >
      {error ? (
        <EmptyState icon="⚠" message="Scan failed" hint={`Open a DUT serial connection, then Scan. (${error})`} />
      ) : data === null ? (
        <EmptyState
          icon="📡"
          message="No scan yet"
          hint={loading ? "Scanning all active VAPs — this can take up to a minute." : "Open a DUT serial connection, then Scan."}
        />
      ) : (
        <>
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", marginBottom: "var(--space-3)" }}>
            {data.recommendations.map((r) => (
              <RecommendationPill key={r.band} rec={r} />
            ))}
            <span style={{ color: "var(--faint)", fontSize: 12, alignSelf: "center" }}>scanned {data.captured_at}</span>
          </div>

          {data.neighbors.length === 0 ? (
            <EmptyState icon="📶" message="No neighboring APs detected" hint={`Scanned ${data.survey_vaps.length} VAP(s).`} />
          ) : (
            <>
              {data.neighbors.length > 5 ? (
                <div className="logscroll-note">
                  Strongest signal first — scroll for more ({data.neighbors.length} total).
                </div>
              ) : null}
              <div className="logscroll">
                <table className="filetable wifitable">
                  <thead>
                    <tr>
                      <th>Band</th>
                      <th>Channel</th>
                      <th>SSID</th>
                      <th>BSSID</th>
                      <th>Signal</th>
                      <th>Security</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedNeighbors.map((n) => (
                      <NeighborRow key={`${n.iface}-${n.bssid}`} n={n} />
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
          <script
            type="application/json"
            id="site-survey-data"
            dangerouslySetInnerHTML={{ __html: JSON.stringify(data.recommendations) }}
          />
        </>
      )}
    </Card>
  );
}

function RecommendationPill({ rec }: { rec: ChannelRecommendation }) {
  const optimal = rec.recommended_channel === rec.current_channel;
  return (
    <span
      className={`pill ${optimal ? "ok" : "warn"}`}
      title={rec.reasoning}
    >
      {optimal ? "✓" : "⚠"} {rec.band}: ch {rec.current_channel}
      {optimal ? "" : ` → ${rec.recommended_channel}`}
    </span>
  );
}

function NeighborRow({ n }: { n: ObservedNeighbor }) {
  return (
    <tr>
      <td>{n.band ?? "—"}</td>
      <td>{n.channel ?? "—"}</td>
      <td className="filetable-name">{n.ssid || <em>hidden</em>}</td>
      <td>{n.bssid}</td>
      <td>{n.signal_dbm === null ? "—" : `${n.signal_dbm} dBm`}</td>
      <td>{n.security ?? "—"}</td>
    </tr>
  );
}
