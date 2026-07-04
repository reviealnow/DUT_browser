import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import { ChannelRecommendation, ObservedNeighbor } from "../api/rest";
import { DEFAULT_DUT_ID } from "../api/dut";
import { ensureSurvey, runSurvey, useSurvey } from "../monitoring/siteSurveyStore";
import { RecommendationPill } from "./RecommendationPill";
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
function channelCounts(
  neighbors: ObservedNeighbor[],
  band: string,
  currentChannel: number,
  recommendedChannel: number,
): [number, number][] {
  const counts = new Map<number, number>();
  for (const n of neighbors) {
    if (n.band === band && n.channel !== null) {
      counts.set(n.channel, (counts.get(n.channel) ?? 0) + 1);
    }
  }
  // Recommended channel is included even when unobserved — an empty channel is
  // exactly what the backend likes to recommend, and it must be visible.
  const channels =
    band === "2.4GHz"
      ? ALL_24G_CHANNELS
      : Array.from(new Set([...counts.keys(), currentChannel, recommendedChannel])).sort((a, b) => a - b);
  return channels.map((ch) => [ch, counts.get(ch) ?? 0]);
}

/**
 * DUT-side site survey + per-band channel recommendation.
 *
 * The scan result lives in a shared module store (siteSurveyStore), not in this
 * card, so it survives the card unmounting on navigation and can be filled by a
 * background prescan the moment serial connects — see runSurvey in the Dashboard
 * open flow. The card just subscribes and displays: it auto-fills the cache once
 * on first open if nothing scanned yet, reuses the cached result on re-entry so
 * the slow (~40s) scan never repeats on navigation, and keeps the previous
 * result on screen during a Re-scan (even if it fails) rather than blanking.
 * Mirrors WifiClientsCard's idiom (Card + Scan action + summary pills + table).
 */
export default function SiteSurveyCard({ dutId = DEFAULT_DUT_ID }: { dutId?: string }) {
  const { result: data, loading, error } = useSurvey(dutId);

  const rerun = () => void runSurvey(dutId);

  // Fill the cache the first time this DUT's section is opened with no result
  // yet — a normal serial connect already prescanned via runSurvey, so this is
  // the fallback for opening Site Survey before connecting. ensureSurvey never
  // re-runs a scan that already succeeded; the ref-gate survives StrictMode.
  const lastDutRef = useRef<string | null>(null);
  useEffect(() => {
    if (lastDutRef.current === dutId) return;
    lastDutRef.current = dutId;
    ensureSurvey(dutId);
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
      {data !== null ? (
        <>
          {error ? (
            <div className="flash" style={{ marginBottom: "var(--space-3)" }}>
              Re-scan failed: {error} — showing the previous scan.
            </div>
          ) : null}
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
              <div className="logscroll logscroll-x">
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
      ) : error ? (
        <EmptyState icon="⚠" message="Scan failed" hint={`Open a DUT serial connection, then Scan. (${error})`} />
      ) : (
        <EmptyState
          icon="📡"
          message="No scan yet"
          hint={loading ? "Scanning all active VAPs — this can take up to a minute." : "Open a DUT serial connection, then Scan."}
        />
      )}
    </Card>
  );
}

/** Per-band vertical column chart of raw SSID count per channel, so the user
 * can eyeball the jam and pick a channel themselves rather than only trusting
 * the recommendation pill above. The busiest channel is called out in red
 * ("Busy"), the recommended one in green ("Best"); the current channel gets a
 * dot on its axis label. Clicking a column previews "what if this radio moved
 * here" using the backend's interference score (occupancy) — one preview per
 * band, since same-band interfaces share the radio's frequency. */
function ChannelUsageChart({ rec, neighbors }: { rec: ChannelRecommendation; neighbors: ObservedNeighbor[] }) {
  const counts = useMemo(
    () => channelCounts(neighbors, rec.band, rec.current_channel, rec.recommended_channel),
    [neighbors, rec.band, rec.current_channel, rec.recommended_channel],
  );
  const max = Math.max(...counts.map(([, c]) => c));
  // Axis headroom: round up past the tallest bar (multiple of 4 keeps the
  // quarter-tick labels integral), so tag/value labels never overflow the top.
  const axisMax = Math.max(4, Math.ceil((max + 1) / 4) * 4);
  const busiest = max > 0 ? counts.find(([, c]) => c === max)![0] : null;

  const [whatIf, setWhatIf] = useState<number | null>(null);
  // A re-scan replaces rec — a stale preview would compare against dead data.
  useEffect(() => setWhatIf(null), [rec]);
  const scoreOf = (ch: number) => rec.occupancy[String(ch)] ?? 0;

  return (
    <div className="chart">
      <div style={{ fontWeight: 600, fontSize: 13 }}>
        {rec.band} channel usage <span style={{ color: "var(--faint)", fontWeight: 400 }}>(SSIDs per channel)</span>
      </div>
      <div className="colchart">
        <div className="colchart-y" aria-hidden="true">
          {[4, 3, 2, 1, 0].map((i) => (
            <span key={i}>{(axisMax * i) / 4}</span>
          ))}
        </div>
        <div className="colchart-cols">
          {counts.map(([ch, count]) => {
            const isCurrent = ch === rec.current_channel;
            const isWhatIf = ch === whatIf && !isCurrent;
            const isRecommended = ch === rec.recommended_channel;
            const isBusiest = ch === busiest && !isRecommended;
            const color = isWhatIf
              ? "var(--accent)"
              : isRecommended
                ? "var(--ok)"
                : isBusiest
                  ? "var(--danger)"
                  : undefined;
            const tag = isWhatIf ? "Try" : isRecommended ? "Best" : isBusiest ? "Busy" : null;
            return (
              <div
                className="colcol"
                key={ch}
                onClick={() => setWhatIf(whatIf === ch ? null : ch)}
                title={`Preview moving ${rec.band} to ch ${ch}`}
              >
                <div className="colcol-track">
                  {tag ? (
                    <span className="colcol-tag" style={{ color }}>
                      {tag}
                    </span>
                  ) : null}
                  <span className="colcol-value" style={color ? { color, fontWeight: 700 } : undefined}>{count}</span>
                  <div
                    className="colcol-bar"
                    style={{
                      height: count > 0 ? `max(2px, ${(count / axisMax) * 100}%)` : 0,
                      background: color,
                    }}
                  />
                </div>
                <div className="colcol-label" style={isWhatIf ? { color: "var(--accent)", fontWeight: 600 } : undefined}>
                  {ch}
                  {isCurrent ? "•" : ""}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <div style={{ display: "flex", gap: "var(--space-3)", fontSize: 11, color: "var(--faint)", flexWrap: "wrap" }}>
        <span>• current (ch {rec.current_channel})</span>
        <span>🟢 recommended (ch {rec.recommended_channel})</span>
        {busiest !== null && busiest !== rec.recommended_channel ? <span>🔴 busiest (ch {busiest})</span> : null}
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
