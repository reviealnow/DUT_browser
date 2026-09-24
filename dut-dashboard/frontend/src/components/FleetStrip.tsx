import { useState } from "react";

import { useFleetMonitor } from "../monitoring/useFleetMonitor";
import { useFleetRecommendations } from "../monitoring/useLastRecommendation";
import { useRemoteRssi } from "../monitoring/RemoteRssiContext";
import FleetCard from "./FleetCard";
import FleetCollapseNotice from "./FleetCollapseNotice";

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
  const [showAll, setShowAll] = useState(false);

  // Nothing here expires. Asked on the bench on 2026-09-22 -- "how long until
  // an inactive DUT clears off Overview?" -- and the answer was never: a
  // registration is persisted and only an explicit Remove takes it out, so a Pi
  // console attached once sits on this strip reading "No DUT" for as long as the
  // registry keeps it. Five cards, two of them dead, is a worse glance view than
  // three.
  //
  // So the strip collapses to the DUTs on a console, borrowing the Fleet page's
  // rule rather than inventing a second one (see FleetSection): this HIDES, it
  // never removes, and the same three guards apply, each of which matters more
  // than the tidying does.
  //
  //  * Only when something IS open. Collapsing to nothing leaves an empty strip
  //    where the DUTs still are, which is worse than the noise it removes.
  //  * Only when it hides something, or the toolbar would announce nothing.
  //  * Never silently -- the count and the way back sit above the cards. A
  //    registered DUT vanishing with neither is indistinguishable from the app
  //    having lost it, and the card carries this DUT's only Connect button, so
  //    what is hidden here is also the way to bring it back.
  //
  // `serialOpen` is the registry's own answer, not a guess from activity: a DUT
  // at a quiet shell prompt is open and reads "idle" (see dutPresence).
  const consoleOpen = fleet.filter((entry) => entry.serialOpen);
  const collapsed = consoleOpen.length > 0 && consoleOpen.length < fleet.length;
  const shown = collapsed && !showAll ? consoleOpen : fleet;

  // Single-DUT (or empty) users have nothing to switch between — hide the strip
  // so Overview isn't cluttered with a redundant one-card row.
  if (fleet.length <= 1) {
    return null;
  }

  return (
    <>
      {/* Only when something is actually hidden (or was, and the reader asked
          for it back). With every card on screen the cards are their own count,
          and a permanent line above them would cost Overview a row to say
          nothing. */}
      {collapsed ? (
        <div className="fleet-section-toolbar fleet-strip-toolbar">
          <FleetCollapseNotice
            fleet={fleet}
            consoleOpen={consoleOpen}
            shown={shown}
            collapsed={collapsed}
            showAll={showAll}
            onShowAll={setShowAll}
            lead="A registration is kept until somebody removes it, so the strip"
          />
        </div>
      ) : null}

      <div className="fleet-strip">
        {shown.map((entry) => (
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
    </>
  );
}
