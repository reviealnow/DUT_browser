import { useCallback, useEffect, useState } from "react";

import { getMeshTopology, humanizeApiError, MeshMember, MeshTopology } from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";
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

/** The host part of a stored management address, or null if it is unusable. */
export function hostOf(mgmtUrl: string): string | null {
  const cleaned = (mgmtUrl || "").trim();
  if (!cleaned) return null;
  try {
    return new URL(cleaned.includes("://") ? cleaned : `https://${cleaned}`).hostname || null;
  } catch {
    return null;
  }
}

/** The DUT this dashboard holds for a mesh member, or null when it holds none. */
export function matchMember(member: MeshMember, fleet: FleetEntry[]): FleetEntry | null {
  if (!member.ip) return null;
  return fleet.find((entry) => hostOf(entry.mgmtUrl) === member.ip) ?? null;
}

/** Every DUT that can be asked: the question goes to its management API. */
export function meshSources(fleet: FleetEntry[]): FleetEntry[] {
  return fleet.filter((entry) => hostOf(entry.mgmtUrl) !== null);
}

function memberLabel(member: MeshMember): string {
  // The device's own words where this repo has no vocabulary for them, so an
  // unrecognised mesh_type reaches the screen rather than being blanked.
  if (member.role === "root") return "Root";
  if (member.role === "node") return "Node";
  return member.mesh_type ?? "—";
}

export default function MeshTopologySection({ fleet }: { fleet: FleetEntry[] }) {
  const { role } = useAuth();
  // The route is admin-only. Drawing a fetch for anyone else is drawing a
  // control that can only answer 403 — the mistake the capture button made once.
  const canRead = role === "admin";
  const sources = meshSources(fleet);
  const [sourceId, setSourceId] = useState<string>("");
  const [topology, setTopology] = useState<MeshTopology | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Whichever eligible DUT is first, until someone picks another. Re-pinned only
  // when the current choice stops being eligible, so a registry refresh does not
  // yank the table back to the top of the list under the reader.
  const selected = sources.some((entry) => entry.id === sourceId) ? sourceId : sources[0]?.id ?? "";

  const load = useCallback(
    async (dutId: string) => {
      if (!dutId) return;
      setLoading(true);
      setError(null);
      try {
        setTopology(await getMeshTopology(dutId));
      } catch (e) {
        // Keep no stale table next to a fresh error: the two together read as
        // "this is what the device says now", which would be a lie.
        setTopology(null);
        setError(humanizeApiError(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // Once per source, on entry. This is an HTTPS read of the DUT's own state —
  // it occupies no serial console, so unlike a backhaul capture it can happen
  // without a click. It still does not poll: the whole point is to be right
  // when the page is opened, not to keep asking.
  useEffect(() => {
    if (!canRead || !selected) return;
    void load(selected);
  }, [canRead, selected, load]);

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
          {topology ? ` Read ${topology.captured_at} from ${topology.mgmt_url}.` : ""}
        </div>
        <div className="mesh-topology-controls">
          {sources.length > 1 ? (
            <label className="mesh-topology-source">
              <span className="setting-hint">Ask</span>
              <select
                value={selected}
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
            onClick={() => void load(selected)}
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
                <th>HOP</th>
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
