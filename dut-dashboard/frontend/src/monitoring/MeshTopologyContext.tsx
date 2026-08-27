import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { getMeshTopology, humanizeApiError, MeshMember, MeshTopology } from "../api/rest";
import { useAuth } from "./AuthContext";
import { FleetEntry } from "./useFleetMonitor";

/**
 * One mesh read, shared by everything on the Fleet page that needs it.
 *
 * The table was the only consumer until the cards started naming a DUT's mesh
 * role. Two consumers must not become two reads: this is an HTTPS GET to the
 * DUT's own management API, and asking the same device twice for the same
 * answer because two components happened to want it is the shape of bug the
 * serial captures are already coalesced against.
 *
 * It also stays **once per source, on entry**. No polling, and no fetch on a
 * card's render: the point is to be right when the page is opened, not to keep
 * asking. That is why the provider wraps the Fleet section rather than the
 * whole app — mounting it around Overview would put a request to the DUT
 * behind a page that never shows a mesh table.
 *
 * Admin-only, because the route is: reading the mesh uses the DUT's management
 * credentials. For anyone else this yields `topology: null` and never fetches,
 * so a card asking for a role gets "no answer" rather than a 403.
 */

export type MeshRole = "root" | "node";

type MeshTopologyValue = {
  /** The last successful read, or null: no read yet, not admin, or it failed. */
  topology: MeshTopology | null;
  /** Which DUT was asked. "" when none is eligible. */
  sourceId: string;
  /** Eligible sources, in registry order. */
  sources: FleetEntry[];
  setSourceId: (dutId: string) => void;
  reload: () => void;
  loading: boolean;
  error: string | null;
  canRead: boolean;
  /**
   * The mesh role this DUT holds, or null when the mesh does not name it.
   *
   * Null is a real answer and must stay distinguishable from "node": a DUT the
   * mesh has never heard of is not a leaf, and a card that printed "Node" for
   * one would be inventing a membership.
   */
  roleFor: (entry: FleetEntry) => MeshRole | null;
};

const MeshTopologyCtx = createContext<MeshTopologyValue | null>(null);

/* The three helpers below moved here from MeshTopology.tsx when the table
   stopped being their only caller. They live beside the state they interpret,
   and the component re-exports them so its tests and any other reader keep one
   definition to find. */

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

export function MeshTopologyProvider({
  fleet,
  children,
}: {
  fleet: FleetEntry[];
  children: React.ReactNode;
}) {
  const { role } = useAuth();
  const canRead = role === "admin";
  const sources = useMemo(() => meshSources(fleet), [fleet]);
  const [wanted, setWanted] = useState<string>("");
  const [topology, setTopology] = useState<MeshTopology | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Whichever eligible DUT is first, until someone picks another. Re-pinned only
  // when the current choice stops being eligible, so a registry refresh does not
  // yank the table back to the top of the list under the reader.
  const sourceId = sources.some((entry) => entry.id === wanted) ? wanted : sources[0]?.id ?? "";

  const load = useCallback(async (dutId: string) => {
    if (!dutId) return;
    setLoading(true);
    setError(null);
    try {
      setTopology(await getMeshTopology(dutId));
    } catch (e) {
      // Keep no stale table next to a fresh error: the two together read as
      // "this is what the device says now", which would be a lie. The cards
      // lose their role for the same reason.
      setTopology(null);
      setError(humanizeApiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!canRead || !sourceId) return;
    void load(sourceId);
  }, [canRead, sourceId, load]);

  const roleFor = useCallback(
    (entry: FleetEntry): MeshRole | null => {
      if (!topology) return null;
      const host = hostOf(entry.mgmtUrl);
      if (!host) return null;
      // Matched on the management address, never on `remote.host`: that is the
      // Pi holding a console, a different machine on a different address, and
      // matching on it would attach a mesh role to the wrong device.
      const member = topology.members.find((m) => (m.ip || "").trim() === host);
      if (!member) return null;
      return member.role === "root" ? "root" : member.role === "node" ? "node" : null;
    },
    [topology],
  );

  const value = useMemo<MeshTopologyValue>(
    () => ({
      topology,
      sourceId,
      sources,
      setSourceId: setWanted,
      reload: () => void load(sourceId),
      loading,
      error,
      canRead,
      roleFor,
    }),
    [topology, sourceId, sources, load, loading, error, canRead, roleFor],
  );

  return <MeshTopologyCtx.Provider value={value}>{children}</MeshTopologyCtx.Provider>;
}

/**
 * The shared mesh read, or a null-shaped value outside the provider.
 *
 * FleetCard renders on Overview too, where no mesh is read at all. Returning a
 * blank value rather than throwing is deliberate: a card there simply has no
 * role to show, which is the truth.
 */
export function useMeshTopology(): MeshTopologyValue {
  const ctx = useContext(MeshTopologyCtx);
  return (
    ctx ?? {
      topology: null,
      sourceId: "",
      sources: [],
      setSourceId: () => {},
      reload: () => {},
      loading: false,
      error: null,
      canRead: false,
      roleFor: () => null,
    }
  );
}
