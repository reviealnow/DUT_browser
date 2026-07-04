import { LastChannelRecommendationResult } from "../api/rest";
import { DutStatus } from "../monitoring/useDutMonitor";
import { FleetEntry, useFleetMonitor } from "../monitoring/useFleetMonitor";
import { useFleetRecommendations } from "../monitoring/useLastRecommendation";
import { FleetBandBadge } from "./BandRecoSummary";
import { EmptyState } from "./shell/Card";

type StatusMeta = { label: string; pill: "ok" | "idle" | "danger" };

const STATUS_META: Record<DutStatus, StatusMeta> = {
  streaming: { label: "Streaming", pill: "ok" },
  idle: { label: "No DUT", pill: "idle" },
  offline: { label: "Offline", pill: "danger" },
};

function formatEventAge(seconds: number | null): string {
  if (seconds === null) {
    return "—";
  }
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  return `${Math.floor(seconds / 60)}m ago`;
}

/**
 * Phase 37: all registered DUTs side-by-side. Each card shows status / latest
 * CPU / crash count / last-event age from a single demuxed `/ws` (see
 * useFleetMonitor) and jumps to that DUT's existing Overview on click. Wi-Fi is
 * intentionally absent — its serial scan is heavy and stays single-DUT on-demand.
 */
export default function FleetSection({ onOpenDut }: { onOpenDut: (dutId: string) => void }) {
  const fleet = useFleetMonitor();
  // Per-DUT last-survey band recommendation, polled from the read-only cache
  // (no scan). Drives the compact per-card band badge.
  const recos = useFleetRecommendations(fleet.map((e) => e.id));

  if (fleet.length === 0) {
    return (
      <EmptyState
        icon="🛰"
        message="No DUTs to show"
        hint="Register a DUT from the switcher, or check the backend is reachable."
      />
    );
  }

  return (
    <div className="fleet-grid">
      {fleet.map((entry) => (
        <FleetCard
          key={entry.id}
          entry={entry}
          reco={recos.get(entry.id)}
          onOpen={() => onOpenDut(entry.id)}
        />
      ))}
    </div>
  );
}

function FleetCard({
  entry,
  reco,
  onOpen,
}: {
  entry: FleetEntry;
  reco: LastChannelRecommendationResult | undefined;
  onOpen: () => void;
}) {
  const meta = STATUS_META[entry.status];
  const cpu = entry.cpuBusyPct === null ? "—" : `${entry.cpuBusyPct}%`;
  const cpuSub =
    entry.cpuBusyPct === null
      ? "Awaiting snapshot"
      : `busy · ${entry.coreCount} core${entry.coreCount === 1 ? "" : "s"}`;

  return (
    <button type="button" className="card fleet-card" onClick={onOpen} title={`Open ${entry.label} overview`}>
      <div className="fleet-card-head">
        <div className="fleet-card-titles">
          <div className="card-title">{entry.label}</div>
          <div className="card-sub">{entry.id}</div>
        </div>
        <span className={`pill ${meta.pill}`}>
          <span className="dot" />
          {meta.label}
        </span>
      </div>
      <div className="fleet-card-cpu">
        <div className="kpi-value">{cpu}</div>
        <div className="kpi-sub">{cpuSub}</div>
      </div>
      <FleetBandBadge reco={reco} />
      <dl className="stat-list">
        <div className="stat-row">
          <dt>Crash events</dt>
          <dd>{entry.crashCount}{entry.crashCount > 0 ? " · since open" : ""}</dd>
        </div>
        <div className="stat-row">
          <dt>Last event</dt>
          <dd>{formatEventAge(entry.lastEventAgeSec)}</dd>
        </div>
        <div className="stat-row">
          <dt>Last snapshot</dt>
          <dd>{entry.lastSnapshotTs ?? "—"}</dd>
        </div>
      </dl>
    </button>
  );
}
