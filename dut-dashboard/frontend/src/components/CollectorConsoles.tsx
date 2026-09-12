import { useCallback, useEffect, useState } from "react";

import {
  attachCollectorConsole,
  BAUD_RATES,
  CollectorConsoles as CollectorConsolesListing,
  CollectorDevice,
  CollectorStatus,
  ConsoleMesh,
  detachCollectorConsole,
  getCollectorConsoles,
  humanizeApiError,
} from "../api/rest";

/**
 * The DUT consoles behind one collector.
 *
 * This is the right-hand half of the bench topology: the collector is reached
 * over SSH, and the DUTs are on *its* serial ports. Attaching one opens an
 * ordinary DUT — same parser, same snapshot ring, same log session — whose
 * transport happens to be `socat` on the far end of that login.
 *
 * Scanning runs commands on the collector, so it happens when the session comes
 * up and then only when asked. The four things it reports are the four that
 * stop a console from opening, and `docs/fleet-remote-nodes.md` currently makes
 * somebody SSH in by hand to check each one.
 */
export default function CollectorConsoles({ collector }: { collector: CollectorStatus }) {
  const [consoles, setConsoles] = useState<CollectorConsolesListing | null>(null);
  const [baudrate, setBaudrate] = useState(115200);
  // Per device, not per panel: whether a DUT is in a mesh is a fact about that
  // DUT, and one toggle governing whichever port you press next is how the
  // wrong console ends up declared meshed.
  const [mesh, setMesh] = useState<Record<string, ConsoleMesh>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scan = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setConsoles(await getCollectorConsoles(collector.id));
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  }, [collector.id]);

  useEffect(() => {
    // Once, when the session comes up. Not on the card's 5s poll: every scan is
    // four commands on somebody's Pi, and nothing about a device list changes
    // often enough to be worth asking that often.
    void scan();
  }, [scan]);

  const meshFor = (device: string): ConsoleMesh =>
    mesh[device] ?? { is_mesh: false, backhaul_iface: null };

  const act = async (run: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await run();
      await scan();
    } catch (err) {
      setError(humanizeApiError(err));
      setBusy(false);
    }
  };

  return (
    <div className="collector-consoles">
      <div className="collector-consoles-head">
        {/* The name the box gave when it answered, falling back to the one it
            was registered under. Those two disagreeing is reported above; here
            the useful one is whichever machine actually has these ports. */}
        <strong>
          DUT consoles on{" "}
          {collector.reported_hostname ?? collector.hostname ?? collector.ip}
        </strong>
        <label className="collector-baud">
          Baud
          <select
            className="input"
            value={baudrate}
            onChange={(event) => setBaudrate(Number(event.target.value))}
            aria-label={`Baud rate for consoles on ${collector.label}`}
          >
            {BAUD_RATES.map((rate) => (
              <option key={rate} value={rate}>
                {rate}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn" disabled={busy} onClick={() => void scan()}>
          {busy ? "Scanning…" : "Rescan"}
        </button>
      </div>

      {consoles?.blockers.length ? (
        <ul className="collector-blockers">
          {consoles.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      ) : null}

      {consoles && consoles.devices.length > 0 ? (
        <ul className="collector-device-list">
          {consoles.devices.map((device) => (
            <li key={device.device} className="collector-device">
              <code>{device.device}</code>
              <span className="card-sub">{describeDevice(device)}</span>
              {device.attached_dut ? (
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={() =>
                    void act(() => detachCollectorConsole(collector.id, device.device))
                  }
                >
                  Detach
                </button>
              ) : (
                <>
                  {/* Declared, never measured: nothing on a console can say
                      whether the DUT behind it is meshed, and a backhaul
                      capture on one that is not is a wrong answer rather than a
                      missing one. */}
                  <label className="collector-mesh">
                    <input
                      type="checkbox"
                      checked={meshFor(device.device).is_mesh}
                      onChange={(event) =>
                        setMesh((current) => ({
                          ...current,
                          [device.device]: {
                            ...meshFor(device.device),
                            is_mesh: event.target.checked,
                          },
                        }))
                      }
                      aria-label={`${device.device} is a mesh node`}
                    />
                    Mesh
                  </label>
                  {meshFor(device.device).is_mesh ? (
                    <input
                      className="input collector-iface"
                      value={meshFor(device.device).backhaul_iface ?? ""}
                      placeholder="ath16"
                      onChange={(event) =>
                        setMesh((current) => ({
                          ...current,
                          [device.device]: {
                            is_mesh: true,
                            backhaul_iface: event.target.value,
                          },
                        }))
                      }
                      aria-label={`Backhaul interface for ${device.device}`}
                    />
                  ) : null}
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() =>
                      void act(() =>
                        attachCollectorConsole(
                          collector.id,
                          device.device,
                          baudrate,
                          meshFor(device.device),
                        ),
                      )
                    }
                  >
                    Attach
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {consoles && consoles.devices.length > 0 ? (
        <div className="setting-hint">
          The backhaul interface is a <strong>fallback</strong>: detection overrides it
          wherever it works, and it is what a root falls back to, since a root cannot name
          its own backhaul VAP from its own console. Leave <em>Mesh</em> clear for a
          standalone AP — the link rows then read <code>Not applicable</code> instead of{" "}
          <code>Not captured</code>.
        </div>
      ) : null}

      {consoles && consoles.busy_check === "unavailable" ? (
        <div className="setting-hint">
          This collector has no <code>fuser</code>, so nothing could check whether a port is
          already in use. Install <code>psmisc</code> to have that answered.
        </div>
      ) : null}

      {error ? <div className="flash">{error}</div> : null}
    </div>
  );
}

/** What one device's state is, in the words that decide the next move. */
function describeDevice(device: CollectorDevice): string {
  if (device.attached_dut) {
    return `Attached here as ${device.attached_dut}`;
  }
  if (device.busy === null) {
    // Never "free": nobody looked. See CollectorDevice in rest.ts.
    return "In use? Not checked";
  }
  if (device.busy) {
    return `In use on the collector${device.held_by ? ` by pid ${device.held_by}` : ""}`;
  }
  return "Free";
}

