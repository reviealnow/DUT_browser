import { useFleetMonitor } from "../monitoring/useFleetMonitor";
import { useFleetRecommendations } from "../monitoring/useLastRecommendation";
import { useRemoteRssi } from "../monitoring/RemoteRssiContext";
import FleetCard from "./FleetCard";

/**
 * Phase 37 / 69: all registered DUTs side-by-side. Each card shows status /
 * latest CPU / crash count / last-event age from a single demuxed `/ws` (see
 * useFleetMonitor). Wi-Fi is intentionally absent — its serial scan is heavy and
 * stays single-DUT on-demand. Phase 66: per-card quick actions (jump to Serial
 * Console; close an open serial session); Phase 67: Connect a remembered DUT.
 *
 * Phase 69: rendered as a horizontal strip at the top of Overview (the dedicated
 * Fleet nav section was removed). Clicking a card selects that DUT — the caller
 * is already on Overview, so no navigation is needed. Hidden entirely when the
 * fleet has one DUT or fewer (nothing to switch between).
 *
 * The Fleet section brings the nav entry back for what a one-row strip cannot
 * hold — a whole capture per node — and both render `FleetCard`, so the strip
 * stays the glance-and-switch view it was made into rather than growing a
 * second, diverging copy of a DUT's state.
 */
export default function FleetStrip({
  onSelectDut,
  onOpenConsole,
}: {
  onSelectDut: (dutId: string) => void;
  onOpenConsole: (dutId: string) => void;
}) {
  const { fleet, refreshRegistry } = useFleetMonitor();
  // Per-DUT last-survey band recommendation, polled from the read-only cache
  // (no scan). Drives the compact per-card band badge.
  const recos = useFleetRecommendations(fleet.map((e) => e.id));
  const rssiState = useRemoteRssi();

  // Single-DUT (or empty) users have nothing to switch between — hide the strip
  // so Overview isn't cluttered with a redundant one-card row.
  if (fleet.length <= 1) {
    return null;
  }

  return (
    <div className="fleet-strip">
      {fleet.map((entry) => (
        <FleetCard
          key={entry.id}
          entry={entry}
          reco={recos.get(entry.id)}
          rssiState={rssiState}
          variant="strip"
          onOpen={() => onSelectDut(entry.id)}
          onConsole={() => onOpenConsole(entry.id)}
          onClosed={refreshRegistry}
        />
      ))}
    </div>
  );
}
