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
 * behind a page that never shows a mesh table. Overview's cards still name a
 * mesh role: `meshRoleFor` falls back to the probe the registry already sent
 * them, which is a stored answer and costs nothing to read.
 *
 * Admin-only, because the route is: reading the mesh uses the DUT's management
 * credentials. For anyone else this yields `topology: null` and never fetches,
 * so a card asking for a role gets "no answer" rather than a 403.
 */

export type MeshRole = "root" | "node";

/**
 * A DUT's mesh role, and which reading produced it.
 *
 * The source is part of the answer rather than decoration, because the two are
 * not equally fresh. The mesh table is read from a device now; the probe is
 * what this DUT last said about itself over its own console, and it carries the
 * moment it said it. Presented identically, a week-old probe would pass for a
 * measurement taken when the page opened.
 */
export type MeshRoleAnswer = {
  role: MeshRole;
  source: "mesh table" | "device probe";
  /** When the probe was taken; null for the live table, which is "now". */
  capturedAt: string | null;
};

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
   * The mesh role this DUT holds, or null when nothing names it.
   *
   * Null is a real answer and must stay distinguishable from "node": a DUT the
   * mesh has never heard of is not a leaf, and a card that printed "Node" for
   * one would be inventing a membership.
   */
  roleFor: (entry: FleetEntry) => MeshRoleAnswer | null;
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

/** This DUT's own row in a list of mesh members, or null if it is not in one.
 *
 *  One rule for both lists on purpose. The live table and the console probe are
 *  parsed by the same backend function into the same member shape, so a second
 *  way of finding a DUT in one of them would be a second answer to one
 *  question — and the two would drift the first time either was corrected. */
function roleInMembers(entry: FleetEntry, members: MeshMember[]): MeshRole | null {
  const host = hostOf(entry.mgmtUrl);
  if (!host) return null;
  // Matched on the management address, never on `remote.host`: that is the Pi
  // holding a console, a different machine on a different address, and matching
  // on it would attach a mesh role to the wrong device.
  const member = members.find((m) => (m.ip || "").trim() === host);
  if (!member) return null;
  return member.role === "root" ? "root" : member.role === "node" ? "node" : null;
}

/**
 * The best answer available about a DUT's mesh role, or null when there is none.
 *
 * The live table wins wherever it names the DUT. Where it does not — nobody is
 * admin, no provider is mounted (Overview mounts none), the read failed, or the
 * table simply does not list this device — the DUT's own stored probe answers
 * instead.
 *
 * The fallback is free, and that is the point: `mesh_probe` already arrives on
 * every fleet entry from the registry, so reading it costs no request. Hoisting
 * the provider to reach Overview would instead have put an HTTPS GET to a DUT
 * behind a page that never shows a mesh table, which is why the provider stays
 * where it is.
 *
 * A probe is also the safer of the two to trust about staleness: the registry
 * drops it when the console behind a DUT changes, so unlike the CPU figures on
 * the same card it cannot survive into a description of different hardware.
 */
export function meshRoleFor(
  entry: FleetEntry,
  topology: MeshTopology | null,
): MeshRoleAnswer | null {
  const live = topology ? roleInMembers(entry, topology.members) : null;
  if (live) {
    return { role: live, source: "mesh table", capturedAt: null };
  }
  const probe = entry.meshProbe;
  // `mesh !== true` is both "the device reported no mesh" and "we asked and
  // could not tell". Neither is a membership to read a role out of, and the
  // second must not be allowed to look like the first.
  if (!probe || probe.mesh !== true) {
    return null;
  }
  const probed = roleInMembers(entry, probe.members);
  return probed ? { role: probed, source: "device probe", capturedAt: probe.captured_at } : null;
}

/**
 * What is known about whether a mesh exists here at all — three answers.
 *
 * Three, not two, and the third is the one that keeps this honest. "Nobody has
 * asked" and "we asked and could not tell" are not "this AP stands alone": the
 * mesh probe already reports those separately for exactly that reason, and a
 * caller that folded them into "no" would be acting on a failed read. Here that
 * would mean hiding DUTs off a page because a console was busy.
 *
 * Both readings are consulted, for the same reason `meshRoleFor` consults both:
 * the table is live and admin-only, the probe is stored and arrives free on
 * every fleet entry, and asking only the first would answer "no mesh" for every
 * engineer who looked at the page.
 *
 * Evidence of members outranks evidence of absence. They can legitimately
 * disagree — one DUT of several standing alone says nothing about the rest.
 */
export type MeshPresence = "members" | "none" | "unknown";

export function meshPresence(
  fleet: FleetEntry[],
  topology: MeshTopology | null,
): MeshPresence {
  if (topology !== null && topology.members.length > 0) {
    return "members";
  }
  if (
    fleet.some((entry) => entry.meshProbe?.mesh === true && entry.meshProbe.members.length > 0)
  ) {
    return "members";
  }
  // A read that came back with an empty list is the device answering, not a
  // failure -- measured on AP6840E-PD1005VMG3KJH9C, which replies
  // `{"mesh_info_list":[],"total_size":0,"error_code":0}` when it is in no
  // mesh. An error code would have produced `mesh: null` instead, and that
  // falls through to "unknown" below.
  if (topology !== null) {
    return "none";
  }
  if (fleet.some((entry) => entry.meshProbe?.mesh === false)) {
    return "none";
  }
  return "unknown";
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
    (entry: FleetEntry): MeshRoleAnswer | null => meshRoleFor(entry, topology),
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
 * The shared mesh read, or a table-less value outside the provider.
 *
 * FleetCard renders on Overview too, where no mesh table is read at all. The
 * blank value rather than a throw is deliberate — but `roleFor` is not blank
 * with it: a card there still answers from the DUT's own stored probe, which
 * arrived with the fleet entry and needs no provider and no request. That is
 * how the same card names a mesh role on both screens without Overview ever
 * talking to a DUT.
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
      roleFor: (entry) => meshRoleFor(entry, null),
    }
  );
}
