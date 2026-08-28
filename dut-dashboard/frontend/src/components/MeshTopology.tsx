

import { MeshMember } from "../api/rest";
import { FleetEntry } from "../monitoring/useFleetMonitor";

/**
 * The mesh as a DUT itself reports it, next to the fleet the dashboard knows.
 *
 * The two disagreed, and that is why this exists: a DUT's console listed a root
 * and a node, while the Fleet showed one card, because every other mesh fact in
 * this app is measured per-console and a member nobody registered has no
 * console to measure. This table is the device's own answer, so it lists those
 * members too — and says, per row, whether this dashboard has anything for it.
 *
 * That last column is the point. A row it cannot match is not a failure to
 * render; it is the finding: something is in the mesh that nothing here can
 * reach. Saying so is more useful than quietly listing five rows that all look
 * equally handled.
 *
 * Matching is by management address and nothing else. A registered node's
 * `remote.host` is the Pi holding its console, not the device — matching on it
 * would tie a mesh member to whichever machine happens to serve its serial
 * port, which is a different device and often a different subnet.
 */

// Moved to monitoring/MeshTopologyContext.tsx when the cards started needing
// them too. Re-exported so there is still exactly one definition, and so this
// module's tests -- which import them from here -- keep working.
export { hostOf, matchMember, meshSources } from "../monitoring/MeshTopologyContext";
import { matchMember, meshSources, useMeshTopology } from "../monitoring/MeshTopologyContext";

function memberLabel(member: MeshMember): string {
  // The device's own words where this repo has no vocabulary for them, so an
  // unrecognised mesh_type reaches the screen rather than being blanked.
  if (member.role === "root") return "Root";
  if (member.role === "node") return "Node";
  return member.mesh_type ?? "—";
}

export default function MeshTopologySection({ fleet }: { fleet: FleetEntry[] }) {
  // The read itself moved to MeshTopologyContext when the cards started needing
  // the same answer. This section no longer owns the fetch -- it renders what
  // the one read produced, so opening this page asks the DUT once whatever is
  // on screen. `fleet` stays a prop because the table names DUTs by it.
  const { topology, sourceId, sources, setSourceId, reload, loading, error, canRead } =
    useMeshTopology();
  void fleet;

  if (sources.length === 0) {
    return (
      <div className="setting-hint">
        No DUT has a management address set, so none can be asked for the mesh table. Set one
        in the Firmware section.
      </div>
    );
  }

  if (!canRead) {
    return (
      <div className="setting-hint">
        Reading the mesh uses the DUT&apos;s management credentials, so it is admin-only.
      </div>
    );
  }

  const members = topology?.members ?? [];

  return (
    <div className="mesh-topology">
      <div className="fleet-section-toolbar">
        <div className="setting-hint">
          Read from the DUT itself over HTTPS, not from a console — so it lists mesh members
          this dashboard has no console for.
          {topology ? ` Read ${topology.captured_at} from ${topology.mgmt_url}.` : ""}{" "}
          {/* Said here as well as in the column heading, because the heading is
              read once and this line is what a reader returns to when the
              number surprises them. */}
          Hop counts are measured from that DUT, not from the root.
        </div>
        <div className="mesh-topology-controls">
          {sources.length > 1 ? (
            <label className="mesh-topology-source">
              <span className="setting-hint">Ask</span>
              <select
                value={sourceId}
                onChange={(e) => setSourceId(e.target.value)}
                aria-label="DUT to ask for the mesh table"
              >
                {sources.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <button
            type="button"
            className="btn"
            onClick={() => reload()}
            disabled={loading}
          >
            {loading ? "Reading…" : "Refresh mesh"}
          </button>
        </div>
      </div>

      {error ? <div className="flash" style={{ color: "var(--danger)" }}>{error}</div> : null}

      {!error && topology && members.length === 0 ? (
        <div className="fleet-detail-empty">
          This DUT reports no mesh members — it is not in a mesh, or its mesh is down.
        </div>
      ) : null}

      {members.length > 0 ? (
        <div className="mesh-topology-table">
          <table className="filetable">
            <thead>
              <tr>
                <th>NODE</th>
                <th>ROLE</th>
                {/* Not "HOP". The device counts hops from ITSELF -- it reports
                    itself as hop 0 and everything else outward from there -- so
                    asking the root and asking a node give the same link
                    different numbers. Measured from both ends of one mesh on
                    2026-08-28; see `_member` in services/mesh_topology.py.

                    A bare "HOP" reads as distance from the root, which is what
                    a hop count means everywhere else, and on a table headed
                    "Mesh topology" nothing on screen contradicted it. The
                    source is named in the line above; the heading now says
                    that is what the number is counted from. */}
                <th>HOPS FROM SOURCE</th>
                <th>ADDRESS</th>
                <th>MAC</th>
                <th>SIGNAL TO PARENT</th>
                <th>IN THIS DASHBOARD</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member, index) => {
                const match = matchMember(member, fleet);
                return (
                  <tr key={member.mac ?? member.ip ?? index}>
                    <td>{member.node ?? (member.node_number ?? "—")}</td>
                    <td>{memberLabel(member)}</td>
                    <td>{member.hop ?? "—"}</td>
                    <td>{member.ip ?? "—"}</td>
                    <td className="fleet-detail-mac">{member.mac ?? "—"}</td>
                    {/* A root's null is "no parent to hear", not a missing
                        measurement, and the two must not both read "—". */}
                    <td>
                      {member.rssi === null
                        ? member.role === "root"
                          ? "n/a — root"
                          : "—"
                        : `${member.rssi} dBm${member.rssi_band ? ` · ${member.rssi_band}` : ""}`}
                    </td>
                    <td>
                      {match ? (
                        `${match.label}${match.serialOpen ? " · console open" : " · no console"}`
                      ) : (
                        <span className="fleet-rssi-na">Not registered here</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
