import { useCallback, useState } from "react";

import { humanizeApiError } from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";
import { useFleetMonitor } from "../monitoring/useFleetMonitor";
import { useFleetRecommendations } from "../monitoring/useLastRecommendation";
import { useRemoteRssi } from "../monitoring/RemoteRssiContext";
import FleetCard from "./FleetCard";
import { Card, EmptyState } from "./shell/Card";

/**
 * The fleet at full width: every registered DUT, and every field of a backhaul
 * capture rather than the two lines the Overview strip has room for.
 *
 * P69 removed the Fleet nav entry because the section "duplicated a full nav
 * slot for what is a glance-and-switch view", and that was true of the cards as
 * they then were — status, CPU, crash count. What the fleet grew since is a
 * measurement: two directions of a mesh backhaul, per child, with SNR and the
 * BSSID that ties a node to its parent. The strip cannot show it and does not
 * try; this is where it goes. The strip stays where it is, still the fastest way
 * to switch DUT from Overview.
 *
 * Nothing here captures on entry. Every capture occupies that DUT's serial
 * console — the section is a view of what was last measured, and asking for a
 * fresh one is a button.
 */
export default function FleetSection({
  onSelectDut,
  onOpenConsole,
}: {
  onSelectDut: (dutId: string) => void;
  onOpenConsole: (dutId: string) => void;
}) {
  const { fleet, refreshRegistry } = useFleetMonitor();
  const recos = useFleetRecommendations(fleet.map((e) => e.id));
  const rssiState = useRemoteRssi();
  const { role } = useAuth();
  const [capturingAll, setCapturingAll] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The capture routes are admin-only, so an engineer is offered the view and
  // not a button that could only answer 403.
  const canCapture = role === "admin";
  const meshOpen = fleet.filter((entry) => entry.remote?.isMesh && entry.serialOpen);

  const captureAll = useCallback(() => {
    setCapturingAll(true);
    setError(null);
    rssiState
      .refreshAll(fleet)
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setCapturingAll(false));
  }, [fleet, rssiState]);

  if (fleet.length === 0) {
    return (
      <Card title="Fleet" subtitle="Every registered DUT">
        <EmptyState icon="🛰" message="No DUTs registered yet." />
      </Card>
    );
  }

  return (
    <>
      <Card
        title="Fleet"
        subtitle={`${fleet.length} registered · ${fleet.filter((e) => e.serialOpen).length} with a console open`}
      >
        <div className="fleet-section-toolbar">
          <div className="setting-hint">
            Backhaul figures are the last capture, not a live feed: reading one occupies that
            DUT&apos;s serial console, so nothing here refreshes on its own.
          </div>
          {canCapture ? (
            <button
              type="button"
              className="btn"
              onClick={captureAll}
              disabled={capturingAll || meshOpen.length === 0}
              title={
                meshOpen.length === 0
                  ? "No mesh node has a console open"
                  : "Capture every mesh node in turn — nodes first, then roots"
              }
            >
              {capturingAll ? "Capturing…" : `Capture all (${meshOpen.length})`}
            </button>
          ) : null}
        </div>
        {error ? <div className="flash" style={{ color: "var(--danger)" }}>{error}</div> : null}
      </Card>

      <div className="fleet-grid">
        {fleet.map((entry) => (
          <FleetCard
            key={entry.id}
            entry={entry}
            reco={recos.get(entry.id)}
            rssiState={rssiState}
            variant="grid"
            onOpen={() => onSelectDut(entry.id)}
            onConsole={() => onOpenConsole(entry.id)}
            onClosed={refreshRegistry}
          />
        ))}
      </div>
    </>
  );
}
