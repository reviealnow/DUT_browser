import { useCallback, useState } from "react";

import { humanizeApiError } from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";
import { FleetEntry, useFleetMonitor } from "../monitoring/useFleetMonitor";
import { useFleetRecommendations } from "../monitoring/useLastRecommendation";
import { useRemoteRssi } from "../monitoring/RemoteRssiContext";
import FleetCard from "./FleetCard";
import MeshTopologySection from "./MeshTopology";
import {
  meshPresence,
  MeshTopologyProvider,
  useMeshTopology,
} from "../monitoring/MeshTopologyContext";
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

  if (fleet.length === 0) {
    return (
      <Card title="Fleet" subtitle="Every registered DUT">
        <EmptyState icon="🛰" message="No DUTs registered yet." />
      </Card>
    );
  }

  return (
    // One mesh read for the whole page. The table and the cards both need it,
    // and two consumers must not become two HTTPS requests to the DUT.
    <MeshTopologyProvider fleet={fleet}>
      <FleetBody
        fleet={fleet}
        refreshRegistry={refreshRegistry}
        onSelectDut={onSelectDut}
        onOpenConsole={onOpenConsole}
      />
    </MeshTopologyProvider>
  );
}

/**
 * The page's body, inside the provider so it can see the mesh read.
 *
 * Split out for exactly that reason and no other: the component that MOUNTS the
 * provider cannot consume it, and deciding whether a mesh exists from the probe
 * alone would ignore an admin's live table — which is the better answer
 * whenever there is one.
 */
function FleetBody({
  fleet,
  refreshRegistry,
  onSelectDut,
  onOpenConsole,
}: {
  fleet: FleetEntry[];
  refreshRegistry: () => Promise<void>;
  onSelectDut: (dutId: string) => void;
  onOpenConsole: (dutId: string) => void;
}) {
  const recos = useFleetRecommendations(fleet.map((e) => e.id));
  const rssiState = useRemoteRssi();
  const { role } = useAuth();
  const { topology } = useMeshTopology();
  const [capturingAll, setCapturingAll] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The capture routes are admin-only, so an engineer is offered the view and
  // not a button that could only answer 403.
  const canCapture = role === "admin";
  // Every DUT a backhaul capture applies to, cabled ones included — the two
  // commands run over a serial console exactly as they do over SSH, and the
  // only DUT that can say "not me" is a remote node an admin declared
  // standalone. `applicable` is the registry's own word for that.
  const meshOpen = fleet.filter((entry) => entry.backhaul.applicable && entry.serialOpen);

  // When a device has ANSWERED that it is in no mesh, this page is not a view of
  // a fleet -- it is one AP, and listing every registry entry beside it invites
  // the reader to look for a topology that does not exist. So it collapses to
  // the DUT actually on a console.
  //
  // Only on "none". `unknown` -- nobody asked, or the console was busy and the
  // probe could not tell -- leaves the page exactly as it was. Hiding DUTs
  // because a read failed would turn a busy serial line into a page that
  // silently forgets half the bench.
  //
  // "A console is open" is the registry's own `serialOpen`, not a guess from
  // activity: a quiet DUT can be open and read "idle".
  //
  // Two more guards, and both matter more than the feature does:
  //
  //  * Only when something IS open. Collapsing to nothing leaves an empty grid
  //    where the DUTs still are, which is worse than the noise it removes.
  //  * Never silently. A registered remote node vanishing off the page with no
  //    count and no way back is indistinguishable from the app losing it, and
  //    a DUT can be registered, reachable and useful while standing alone --
  //    being outside a mesh is not a reason to stop showing it, only a reason
  //    to stop leading with it.
  const consoleOpen = fleet.filter((entry) => entry.serialOpen);
  const collapsed =
    meshPresence(fleet, topology) === "none" &&
    consoleOpen.length > 0 &&
    consoleOpen.length < fleet.length;
  const shown = collapsed && !showAll ? consoleOpen : fleet;
  const hidden = fleet.length - shown.length;

  const captureAll = useCallback(() => {
    setCapturingAll(true);
    setError(null);
    rssiState
      .refreshAll(fleet)
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setCapturingAll(false));
  }, [fleet, rssiState]);

  return (
    <>
      {/* Above the cards on purpose. The cards are what this dashboard has
          measured; this is what the mesh actually contains, and when the two
          disagree the reader needs to meet the fuller list first. */}
      <Card title="Mesh topology" subtitle="As the DUT itself reports it">
        <MeshTopologySection fleet={fleet} />
      </Card>

      {error ? <div className="flash" style={{ color: "var(--danger)" }}>{error}</div> : null}

      {/* The count, the caveat and the capture button all describe the grid
          below, so they sit against it rather than in a card of their own. That
          card held nothing else, and its title repeated the page's: three
          headings stacked up before any data, one of them empty. */}
      <div className="fleet-section-toolbar fleet-grid-toolbar">
        <div className="setting-hint">
          <strong className="fleet-count">
            {fleet.length} registered · {consoleOpen.length} with a console open.
          </strong>{" "}
          {/* Said where the count is, because the count is what stops adding up
              otherwise: a reader who sees "3 registered" above one card needs
              the reason in the same breath, not two paragraphs away. */}
          {collapsed && !showAll ? (
            <>
              The DUT reports no mesh, so the grid shows the{" "}
              {consoleOpen.length === 1 ? "DUT" : "DUTs"} on a console and{" "}
              {hidden === 1 ? "hides 1 other" : `hides ${hidden} others`}.{" "}
              <button type="button" className="linklike" onClick={() => setShowAll(true)}>
                Show all
              </button>
              .{" "}
            </>
          ) : null}
          {collapsed && showAll ? (
            <>
              Showing every registered DUT.{" "}
              <button type="button" className="linklike" onClick={() => setShowAll(false)}>
                Show only the console
              </button>
              .{" "}
            </>
          ) : null}
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
                ? "No DUT with a backhaul to measure has a console open"
                : "Capture every DUT in turn — nodes first, then roots"
            }
          >
            {capturingAll ? "Capturing…" : `Capture all (${meshOpen.length})`}
          </button>
        ) : null}
      </div>

      <div className="fleet-grid">
        {shown.map((entry) => (
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
