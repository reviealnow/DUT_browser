import { useCallback, useState } from "react";

import {
  closeSerial,
  connectRemoteNode,
  disconnectRemoteNode,
  humanizeApiError,
  LastChannelRecommendationResult,
  openSerial,
  RemoteRssiResult,
} from "../api/rest";
import { ROLE_RANK, useAuth } from "../monitoring/AuthContext";
import { runConnectCaptures } from "../monitoring/siteSurveyStore";
import { DutStatus } from "../monitoring/useDutMonitor";
import { useMeshTopology } from "../monitoring/MeshTopologyContext";
import { FleetEntry } from "../monitoring/useFleetMonitor";
import { RemoteRssiState } from "../monitoring/RemoteRssiContext";
import { FleetBandBadge } from "./BandRecoSummary";

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
 * One DUT's card, in both places the fleet is shown.
 *
 * `variant` changes the frame and how much of a capture is unfolded, never what
 * the card claims: the strip on Overview is a glance-and-switch row, and the
 * Fleet section has the width to show the measurement whole. Two components
 * would have been two accounts of the same DUT, and the next correction would
 * have landed in one of them.
 */
export default function FleetCard({
  entry,
  reco,
  rssiState,
  variant,
  onOpen,
  onConsole,
  onClosed,
}: {
  entry: FleetEntry;
  reco: LastChannelRecommendationResult | undefined;
  rssiState: RemoteRssiState;
  variant: "strip" | "grid";
  onOpen: () => void;
  onConsole: () => void;
  onClosed: () => Promise<void>;
}) {
  const { role } = useAuth();
  // Gate each control on the role its own route needs, not on one blanket
  // check: the serial router is engineer, every /api/fleet route is admin, and
  // the Serial Console section itself is engineer. A button that can only
  // answer 403 is worse than no button (the same call DutSwitcher makes).
  // This was already true of the strip before the Fleet section existed —
  // guests were being shown Connect, Close serial and Refresh RSSI.
  const canDrive = ROLE_RANK[role] >= ROLE_RANK[entry.remote ? "admin" : "engineer"];
  const canOpenConsole = ROLE_RANK[role] >= ROLE_RANK["engineer"];
  // The capture is /api/fleet, which is admin whichever console it runs on —
  // so it cannot borrow `canDrive`, which is engineer for a cabled DUT and
  // would hand an engineer a button that can only answer 403.
  const canCapture = ROLE_RANK[role] >= ROLE_RANK["admin"];
  const [closing, setClosing] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error` on purpose. A substituted port is not a failure —
  // the connect worked — but it IS a change the operator did not ask for, and
  // silently opening a different device than the card advertises is the kind of
  // thing that is only noticed much later, when a capture is filed under a name
  // nobody expected. So it is said out loud, in a colour that is not alarm.
  const [portNote, setPortNote] = useState<string | null>(null);
  const rssi = rssiState.get(entry);
  const capturingRssi = rssiState.capturing(entry.id);
  // What the two backhaul rows say when there is no number to show. Each of
  // these is a different state and the card has to keep them apart: nobody has
  // measured; measured and this DUT is the root; measured and nothing here
  // belongs to a mesh at all. Reading them all as "Not captured" is how a
  // healthy link and a standalone AP came to look the same.
  const uplinkText =
    !rssi.applicable
      ? "Not applicable"
      : rssi.role === "root"
        ? "None — this is the root"
        : rssi.uplink && rssi.uplink.rssi !== null
          ? `${rssi.uplink.rssi} dBm · ${rssi.uplink.rssi_band}`
          : rssi.captured && rssi.role === null
            ? "None — no parent found"
            : "Not captured";
  const childrenText =
    !rssi.applicable
      ? "Not applicable"
      : rssi.downlink
        ? `${
            rssi.downlink.peers.length === 0
              ? "None"
              : rssi.downlink.peers
                  .map((p) => (p.rssi === null ? "—" : `${p.rssi} dBm`))
                  .join(" · ")
          }${
            // An interface nobody verified is a backhaul gets said out loud,
            // with the SSID it serves: on the bench a configured VAP was
            // carrying an ordinary laptop.
            rssi.downlink.source === "configured"
              ? ` · ${rssi.downlink.iface} configured${
                  rssi.downlink.essid ? ` (${rssi.downlink.essid})` : ""
                }`
              : ""
          }`
        : !rssi.captured
          ? "Not captured"
          : "No backhaul VAP identified";
  // Grey for a settled non-answer — not applicable, no parent — but not for
  // "Not captured", which is a prompt to press the button rather than a state
  // of the DUT.
  /* Four things this row can say, and they are four different claims:
     nobody has asked yet; the device listed members; the device answered with
     an empty list; we asked and could not tell. Collapsing the last two into
     one "no" is the failure this whole feature is about — it would print "no
     mesh" over a device that merely failed to answer. */
  const probe = entry.meshProbe;
  const probeText = !probe
    ? "Not probed"
    : probe.mesh === true
      ? `${probe.members.length} member${probe.members.length === 1 ? "" : "s"} reported`
      : probe.mesh === false
        ? "No mesh on this device"
        : "Could not tell";
  // Muted for every state that is not a positive answer, including "could not
  // tell" — which must not read like a measurement.
  const probeMuted = !probe || probe.mesh !== true;

  const uplinkMuted = !rssi.applicable || rssi.role === "root" || (rssi.captured && rssi.role === null);
  const childrenMuted = !rssi.applicable || (rssi.captured && !rssi.downlink);
  const meta = STATUS_META[entry.status];
  // The live mesh table where one was read — Fleet page, admin — and this DUT's
  // own stored probe everywhere else, which is what lets the same card name a
  // role on Overview without that page ever addressing a DUT. Still null when
  // neither names it, and the line below omits the role rather than guessing.
  const meshRole = useMeshTopology().roleFor(entry);
  const cpu = entry.cpuBusyPct === null ? "—" : `${entry.cpuBusyPct}%`;
  // The CPU figures come from the last snapshot — whatever hardware that was
  // recorded on. A registry entry outlives the device cabled to it, so a card
  // can carry an 840E's four cores under the name of the 420E now on the desk,
  // with nothing on screen saying so. `Last snapshot` is the only clue and it
  // sits six rows further down.
  //
  // Both numbers have to be real for this to mean anything: no model (no
  // console yet) and no snapshot are each "no answer", not a disagreement.
  const coresDisagree =
    entry.modelCores !== null && entry.coreCount > 0 && entry.coreCount !== entry.modelCores;
  const cpuSub =
    entry.cpuBusyPct === null
      ? "Awaiting snapshot"
      : `busy · ${entry.coreCount} core${entry.coreCount === 1 ? "" : "s"}`;
  const coresNote = coresDisagree
    ? `${entry.model} has ${entry.modelCores} — this reading is another device's`
    : null;

  const onCloseSerial = useCallback(() => {
    if (!window.confirm(`Close the serial session on ${entry.label}?`)) {
      return;
    }
    setClosing(true);
    setError(null);
    setPortNote(null);
    (entry.remote ? disconnectRemoteNode(entry.id) : closeSerial(entry.id))
      .then(() => onClosed())
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setClosing(false));
  }, [entry.id, entry.label, onClosed]);

  const lastSerial = entry.lastSerial;
  const onConnect = useCallback(() => {
    if (!lastSerial && !entry.remote) {
      return;
    }
    setConnecting(true);
    setError(null);
    setPortNote(null);
    // Reopen with the remembered params. On success refresh the registry (so the
    // card flips to the open/Close state) and kick the connect-time captures
    // (P58 prescan + P73 context), exactly like a console-driven open.
    const connect = entry.remote
      ? connectRemoteNode(entry.id)
      : openSerial(
          { port: lastSerial!.port, baudrate: lastSerial!.baudrate, mode: "serial" },
          entry.id,
        ).then((result) => {
          // Only set when the backend opened a port other than the one asked
          // for — a USB adapter renumbers on every replug, and the remembered
          // name goes stale the moment the desk is recabled.
          setPortNote(result.port_note ?? null);
        });
    connect
      .then(() => onClosed())
      .then(() => {
        if (entry.remote) {
          return rssiState.refresh(entry);
        }
        void runConnectCaptures(entry.id);
      })
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setConnecting(false));
  }, [entry.id, entry.remote, lastSerial, onClosed, rssiState]);

  const onRefreshRssi = useCallback(() => {
    setError(null);
    rssiState.refresh(entry).catch((e) => setError(humanizeApiError(e)));
  }, [entry, rssiState]);

  // Where a DUT's console is attached — this machine, or a remote Pi over SSH —
  // is the one distinction this strip exists to make, so the two get the
  // palette's two furthest-apart colours rather than a hue per id. A hashed hue
  // put "mesh1" and "ap2" on neighbouring purples; telling them apart at a
  // glance is the whole job, and it was not doing it.
  //
  // Note this is not a DUT's mesh role. Console location and mesh role are
  // independent: a mesh root can hang off a Pi and a mesh node can be cabled to
  // this machine. Whether a DUT has an uplink is measured, not inferred from
  // which card it is.
  const cardStyle = entry.remote
    ? { borderTopColor: "var(--ok, #0c7a43)" }
    : { borderTopColor: "var(--accent, #1565c0)" };

  return (
    <div className={`card fleet-card fleet-card--${variant}`} style={cardStyle}>
      <button
        type="button"
        className="fleet-card-main"
        onClick={onOpen}
        title={`Select ${entry.label}`}
      >
        <div className="fleet-card-head">
          <div className="fleet-card-titles">
            <div className="card-title">{entry.label}</div>
            <div className="card-sub">{entry.id}</div>
            {/* Where the console is attached — the machine, not the device.
                Highlighted because it is the line that says whether a reading
                came off this desk or off a Pi in another room, and it used to
                read as quietly as the id above it. */}
            <div className="card-sub fleet-card-origin">
              {entry.remote ? `Remote via ${entry.remote.host}:${entry.remote.port}` : "Origin_Server"}
            </div>
            {/* What the device is, and where it sits in the mesh. Both are
                answers the dashboard only sometimes has, and the line says
                which it is missing rather than filling in a plausible one:
                the model needs a console to have been opened, the role needs
                either an admin's mesh read or a probe on this DUT's console.

                A role read from a stored probe says so on hover rather than in
                the line. The two sources are not equally fresh — one was read
                just now, one is as old as its timestamp — and printing them
                identically with no way at all to tell them apart is the kind of
                quiet equivalence this card exists to avoid. */}
            <div
              className="card-sub fleet-card-identity"
              title={
                meshRole?.source === "device probe"
                  ? `Mesh role as this device reported it at ${meshRole.capturedAt}`
                  : undefined
              }
            >
              {entry.model ?? "AP6"}
              {meshRole ? ` Mesh ${meshRole.role === "root" ? "Root" : "Node"}` : ""}
            </div>
          </div>
          <span className={`pill ${meta.pill}`}>
            <span className="dot" />
            {meta.label}
          </span>
        </div>
        <div className="fleet-card-cpu">
          <div className="kpi-value">{cpu}</div>
          <div className={`kpi-sub${coresNote ? " kpi-sub-mismatch" : ""}`}>{cpuSub}</div>
          {/* Said out loud rather than left to the reader to spot: the number
              above is a measurement of hardware this card is no longer about.
              Not styled as an alarm — nothing is broken, the reading is simply
              older than the device. */}
          {coresNote ? <div className="kpi-note">{coresNote}</div> : null}
        </div>
        <FleetBandBadge reco={reco} />
        <dl className="stat-list fleet-remote-facts">
          {entry.remote ? (
            <>
              <div className="stat-row">
                {/* Nothing here probes the Pi: this is whether the backend is
                    holding an SSH console open, so it must not be worded as
                    reachability, and "not connected" is a resting state rather
                    than a fault. Whether bytes are actually arriving is the
                    console row below. */}
                <dt>SSH session</dt>
                <dd className={entry.serialOpen ? "fleet-fact-ok" : "fleet-fact-idle"}>
                  {entry.serialOpen ? "Connected" : "Not connected"}
                </dd>
              </div>
              <div className="stat-row">
                <dt>Node console</dt>
                <dd>{meta.label}</dd>
              </div>
            </>
          ) : null}
          {/* Two directions, two rows, because they answer different questions
              and come from different commands. One "Backhaul RSSI" number
              could not say which way it pointed.

              Shown for a cabled DUT too: the measurement is the same two
              console commands, and the fleet's root is regularly the DUT on
              this desk — which used to be the one device whose backhaul could
              never be shown. What a cabled DUT does not carry is an admin's
              word that it is meshed, so its rows say what was measured and
              stop there. */}
          <div className="stat-row">
            <dt>Uplink to parent</dt>
            {/* A root has no parent, and a capture that parsed its VAPs
                established that. Saying "Not captured" there would report a
                known state as a missing one. */}
            <dd className={uplinkMuted ? "fleet-rssi-na" : undefined}>{uplinkText}</dd>
          </div>
          <div className="stat-row">
            <dt>Children on backhaul</dt>
            <dd className={childrenMuted ? "fleet-rssi-na" : undefined}>{childrenText}</dd>
          </div>
          {/* What the DEVICE said, beside what a console measured. Kept as its
              own row rather than folded into the two above: those are readings
              off this DUT's radios, this is the device's own answer, and it is
              the only one that can mention members no console here reaches.
              It also never overwrites the admin's `is_mesh` — a DUT declared
              standalone that reports two mesh members is exactly what someone
              needs to see, and one merged row would hide it. */}
          <div className="stat-row">
            <dt>Mesh (device says)</dt>
            <dd className={probeMuted ? "fleet-rssi-na" : undefined}>{probeText}</dd>
          </div>
        </dl>
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
      {variant === "grid" ? <FleetLinkDetail rssi={rssi} /> : null}
      <div className="fleet-card-actions">
        {canOpenConsole ? (
          <button
            type="button"
            className="btn"
            title={`Open ${entry.label} serial console`}
            onClick={onConsole}
          >
            Console
          </button>
        ) : null}
        {!canDrive ? null : entry.serialOpen ? (
          <button
            type="button"
            className="btn"
            disabled={closing}
            title={`Close the serial session on ${entry.label}`}
            onClick={onCloseSerial}
          >
            {closing ? "Closing…" : "Close serial"}
          </button>
        ) : lastSerial || entry.remote ? (
          <button
            type="button"
            className="btn"
            disabled={connecting}
            title={entry.remote
              ? `Connect ${entry.label} through ${entry.remote.host}`
              : `Connect ${entry.label} on ${lastSerial!.port} @ ${lastSerial!.baudrate}`}
            onClick={onConnect}
          >
            {connecting ? "Connecting…" : "Connect"}
          </button>
        ) : (
          <button
            type="button"
            className="btn"
            disabled
            title="No remembered serial parameters — open this DUT once from the Serial Console."
          >
            Connect
          </button>
        )}
        {canCapture ? (
          <button
            type="button"
            className="btn"
            disabled={!entry.serialOpen || capturingRssi || !rssi.applicable}
            onClick={onRefreshRssi}
            title={
              rssi.applicable
                ? "Capture backhaul RSSI now"
                : "Standalone AP has no mesh backhaul RSSI"
            }
          >
            {capturingRssi ? "Reading…" : "Refresh RSSI"}
          </button>
        ) : null}
        {error ? <span className="flash" style={{ color: "var(--danger)" }}>{error}</span> : null}
        {portNote ? (
          <span className="flash fleet-port-note" role="status">{portNote}</span>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The capture, unfolded — every field the backend measured.
 *
 * The strip compresses a capture into two lines: the uplink to one number, and
 * every child's RSSI into one joined string. Three fields the DUT reports and
 * `/api/fleet/nodes/{id}/rssi` returns had no reader anywhere in the frontend —
 * the uplink's `snr`, `peer_mac` and `radio_band` — and per-child RSSI, the
 * measurement that says *which* child is hearing badly, was legible only for
 * one child. Both are why this section exists.
 */
function FleetLinkDetail({ rssi }: { rssi: RemoteRssiResult }) {
  if (!rssi.applicable) {
    return null;
  }
  const uplink = rssi.uplink;
  const downlink = rssi.downlink;
  if (!rssi.captured) {
    return (
      <div className="fleet-detail fleet-detail-empty">
        No capture yet — press Refresh RSSI with the console open.
      </div>
    );
  }
  if (!uplink && !downlink) {
    // Measured, and neither direction is there. Not the same sentence as "no
    // capture yet", and for a cabled DUT it is the ordinary reading of a
    // standalone AP — which must not be dressed up as a mesh root.
    return (
      <div className="fleet-detail fleet-detail-empty">
        Captured: no parent, and nothing names one of this DUT&apos;s VAPs as a mesh backhaul.
        Either it is standalone, or no node that joins it has been captured yet.
      </div>
    );
  }
  return (
    <div className="fleet-detail">
      <div className="fleet-detail-block">
        <div className="setting-label">Uplink to parent</div>
        {rssi.role === "root" ? (
          <div className="setting-hint">None — this is the root of the mesh.</div>
        ) : !uplink ? (
          <div className="setting-hint">Not captured.</div>
        ) : (
          <dl className="stat-list">
            <div className="stat-row">
              <dt>Interface</dt>
              <dd>{uplink.iface}</dd>
            </div>
            <div className="stat-row">
              <dt>Signal</dt>
              <dd>
                {uplink.rssi === null ? "—" : `${uplink.rssi} dBm`}
                {uplink.rssi_band ? ` · ${uplink.rssi_band}` : ""}
              </dd>
            </div>
            {/* Measured by the same command as the RSSI and carried in the same
                response; until now nothing rendered it. */}
            <div className="stat-row">
              <dt>SNR</dt>
              <dd>{uplink.snr === null ? "—" : `${uplink.snr} dB`}</dd>
            </div>
            <div className="stat-row">
              <dt>Band</dt>
              <dd>{uplink.radio_band ?? "—"}</dd>
            </div>
            <div className="stat-row">
              <dt>Backhaul SSID</dt>
              <dd>{uplink.essid ?? "—"}</dd>
            </div>
            <div className="stat-row">
              {/* The parent's BSSID. It is what identifies a root's backhaul
                  VAP, which the root cannot name from its own console — so it
                  is the field that ties two cards together. */}
              <dt>Parent BSSID</dt>
              <dd className="fleet-detail-mac">{uplink.peer_mac ?? "—"}</dd>
            </div>
          </dl>
        )}
      </div>

      <div className="fleet-detail-block">
        <div className="setting-label">Children on backhaul</div>
        {!downlink ? (
          <div className="setting-hint">Not captured.</div>
        ) : (
          <>
            <div className="setting-hint">
              {downlink.iface}
              {downlink.essid ? ` · ${downlink.essid}` : ""} ·{" "}
              {downlink.source === "detected"
                ? "detected backhaul"
                : "configured — nobody verified this interface is a backhaul"}
            </div>
            {downlink.peers.length === 0 ? (
              <div className="setting-hint">No children associated.</div>
            ) : (
              <table className="filetable fleet-peer-table">
                <thead>
                  <tr>
                    <th>CHILD BSSID</th>
                    <th>SIGNAL</th>
                    <th>QUALITY</th>
                  </tr>
                </thead>
                <tbody>
                  {downlink.peers.map((peer) => (
                    <tr key={peer.mac}>
                      <td className="fleet-detail-mac">{peer.mac}</td>
                      <td>{peer.rssi === null ? "—" : `${peer.rssi} dBm`}</td>
                      <td>{peer.rssi_band ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  );
}
