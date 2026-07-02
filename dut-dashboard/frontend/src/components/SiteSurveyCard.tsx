import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

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

// 2.4GHz always shows the full channel 1-13 grid (including empty channels —
// "nothing here" is exactly what you want to see when picking a clear one).
// 5/6GHz have hundreds of possible channels, so only ones actually seen in the
// scan (plus the current channel) are shown, ascending.
const ALL_24G_CHANNELS = Array.from({ length: 13 }, (_, i) => i + 1);

/** Raw neighbor (SSID) count per channel for one band — NOT the signal-weighted
 * occupancy score used for the recommendation pill. This is the plain "how many
 * APs are on this channel" number so the user can read the jam themselves. */
function channelCounts(neighbors: ObservedNeighbor[], band: string, currentChannel: number): [number, number][] {
  const counts = new Map<number, number>();
  for (const n of neighbors) {
    if (n.band === band && n.channel !== null) {
      counts.set(n.channel, (counts.get(n.channel) ?? 0) + 1);
    }
  }
  const channels =
    band === "2.4GHz"
      ? ALL_24G_CHANNELS
      : Array.from(new Set([...counts.keys(), currentChannel])).sort((a, b) => a - b);
  return channels.map((ch) => [ch, counts.get(ch) ?? 0]);
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

          <div className="grid" style={{ marginBottom: "var(--space-4)" }}>
            {data.recommendations.map((rec) => (
              <ChannelUsageChart key={rec.band} rec={rec} neighbors={data.neighbors} />
            ))}
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

/** Per-band bar chart of raw SSID count per channel, so the user can eyeball
 * the jam and pick a channel themselves rather than only trusting the
 * recommendation pill above. The current channel's bar is called out.
 * Clicking a bar previews "what if this radio moved here" using the
 * backend's interference score (occupancy) — one preview per band, since
 * same-band interfaces share the radio's frequency. */
function ChannelUsageChart({ rec, neighbors }: { rec: ChannelRecommendation; neighbors: ObservedNeighbor[] }) {
  const counts = useMemo(
    () => channelCounts(neighbors, rec.band, rec.current_channel),
    [neighbors, rec.band, rec.current_channel],
  );
  const max = Math.max(1, ...counts.map(([, c]) => c));

  const [whatIf, setWhatIf] = useState<number | null>(null);
  // A re-scan replaces rec — a stale preview would compare against dead data.
  useEffect(() => setWhatIf(null), [rec]);
  const scoreOf = (ch: number) => rec.occupancy[String(ch)] ?? 0;

  return (
    <div className="chart">
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: "var(--space-2)" }}>
        {rec.band} channel usage <span style={{ color: "var(--faint)", fontWeight: 400 }}>(SSIDs per channel)</span>
      </div>
      <div className="barrows">
        {counts.map(([ch, count]) => {
          const isCurrent = ch === rec.current_channel;
          const isRecommended = ch === rec.recommended_channel && !isCurrent;
          const isWhatIf = ch === whatIf && !isCurrent;
          return (
            <div
              className="barrow"
              key={ch}
              onClick={() => setWhatIf(whatIf === ch ? null : ch)}
              style={{ cursor: "pointer" }}
              title={`Preview moving ${rec.band} to ch ${ch}`}
            >
              <div className="barrow-label" style={isWhatIf ? { color: "var(--accent)", fontWeight: 600 } : undefined}>
                Ch {ch}
                {isCurrent ? " •" : ""}
              </div>
              <div className="barrow-track">
                <div
                  className="barrow-fill"
                  style={{
                    width: `${(count / max) * 100}%`,
                    background: isCurrent ? "var(--warn)" : isWhatIf ? "var(--accent)" : isRecommended ? "var(--ok)" : undefined,
                  }}
                />
              </div>
              <div className="barrow-value">{count}</div>
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: "var(--space-3)", fontSize: 11, color: "var(--faint)", marginTop: "var(--space-2)", flexWrap: "wrap" }}>
        <span>🟠 current (ch {rec.current_channel})</span>
        {rec.recommended_channel !== rec.current_channel ? (
          <span>🟢 recommended (ch {rec.recommended_channel})</span>
        ) : null}
        {whatIf === null ? (
          <span>click a bar to preview a move</span>
        ) : (
          <WhatIfPreview
            band={rec.band}
            channel={whatIf}
            score={scoreOf(whatIf)}
            currentChannel={rec.current_channel}
            currentScore={scoreOf(rec.current_channel)}
            onClear={() => setWhatIf(null)}
          />
        )}
      </div>
    </div>
  );
}

/** Interference-score comparison for a hypothetical channel move. Scores are
 * the backend's signal-weighted occupancy (adjacent-channel aware on 2.4GHz),
 * NOT the raw SSID counts drawn in the bars — same source as the pill. */
function WhatIfPreview({
  band,
  channel,
  score,
  currentChannel,
  currentScore,
  onClear,
}: {
  band: string;
  channel: number;
  score: number;
  currentChannel: number;
  currentScore: number;
  onClear: () => void;
}) {
  const same = channel === currentChannel;
  const better = score < currentScore;
  const verdict = same
    ? "already the current channel"
    : `interference ${score < currentScore ? "↓" : score > currentScore ? "↑" : "="} ${score.toFixed(1)} vs ${currentScore.toFixed(1)} now`;
  return (
    <Fragment>
      <span className={`pill ${same || better ? "ok" : "warn"}`} style={{ fontSize: 11 }}>
        what-if {band} → ch {channel}: {verdict}
      </span>
      <button className="btn" style={{ fontSize: 11, padding: "0 var(--space-2)" }} onClick={onClear}>
        ✕
      </button>
    </Fragment>
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
