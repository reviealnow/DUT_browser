import { ChannelRecommendation, LastChannelRecommendationResult } from "../api/rest";
import { useLastRecommendation } from "../monitoring/useLastRecommendation";
import { RecommendationPill } from "./RecommendationPill";
import { EmptyState } from "./shell/Card";

const BAND_ORDER: Record<string, number> = { "2.4GHz": 0, "5GHz": 1, "6GHz": 2 };

function byBand(a: ChannelRecommendation, b: ChannelRecommendation): number {
  return (BAND_ORDER[a.band] ?? 99) - (BAND_ORDER[b.band] ?? 99);
}

/** Number of bands where a clearer channel than the current one exists. */
export function warnCount(recs: ChannelRecommendation[]): number {
  return recs.filter((r) => r.recommended_channel !== r.current_channel).length;
}

/** "just now" / "3m ago" / "2h ago" from an ISO timestamp; "" if unparseable. */
function formatAge(iso: string | null): string {
  if (!iso) {
    return "";
  }
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return "";
  }
  const sec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (sec < 60) {
    return "just now";
  }
  if (sec < 3600) {
    return `${Math.floor(sec / 60)}m ago`;
  }
  return `${Math.floor(sec / 3600)}h ago`;
}

/**
 * Overview mini-card body: the selected DUT's per-band channel recommendation
 * from the cached last survey (no scan). Shows one pill per band plus staleness;
 * an empty state until the DUT has been surveyed once (a serial connect
 * prescans automatically).
 */
export function OverviewBandReco({ dutId }: { dutId: string }) {
  const { recommendations, captured_at, cached } = useLastRecommendation(dutId);

  if (!cached || recommendations.length === 0) {
    return (
      <EmptyState
        icon="📡"
        message="No survey yet"
        hint="Connect serial to prescan automatically, or open Site Survey and Re-scan."
      />
    );
  }

  const recs = [...recommendations].sort(byBand);
  const age = formatAge(captured_at);
  return (
    <div style={{ display: "grid", gap: "var(--space-3)" }}>
      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        {recs.map((r) => (
          <RecommendationPill key={r.band} rec={r} />
        ))}
      </div>
      {age ? <span style={{ color: "var(--faint)", fontSize: 12 }}>scanned {age}</span> : null}
    </div>
  );
}

/**
 * Compact Fleet badge: a single pill summarising a DUT's last survey — amber
 * "N band(s) to tune" when clearer channels exist, green "channels ok" when all
 * bands are optimal. Renders nothing when the DUT has never been surveyed (keeps
 * cards for un-scanned DUTs clean).
 */
export function FleetBandBadge({ reco }: { reco: LastChannelRecommendationResult | undefined }) {
  if (!reco || !reco.cached || reco.recommendations.length === 0) {
    return null;
  }
  const n = warnCount(reco.recommendations);
  if (n === 0) {
    return (
      <span className="pill ok">
        <span className="dot" />
        channels ok
      </span>
    );
  }
  return (
    <span className="pill warn">
      <span className="dot" />
      {n} band{n === 1 ? "" : "s"} to tune
    </span>
  );
}
