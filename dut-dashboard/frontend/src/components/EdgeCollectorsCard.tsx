import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  attachCollectorConsole,
  BAUD_RATES,
  CollectorConsoles,
  CollectorDevice,
  CollectorStatus,
  ConsoleMesh,
  configureCollector,
  connectCollector,
  detachCollectorConsole,
  disconnectCollector,
  getCollectorConsoles,
  getCollectors,
  humanizeApiError,
  MAX_COLLECTORS,
  removeCollector,
  setCollectorPassword,
} from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";
import { Card } from "./shell/Card";
import LiveDot from "./shell/LiveDot";

/**
 * Admin-only registration and login for edge log collectors.
 *
 * A collector is the Raspberry Pi as a machine, not as somebody's console. It
 * sits in the middle of this bench:
 *
 *   LAN DUT console  <<Server>>  Raspberry Pi  (ssh)  >>  LAN DUT console
 *
 * so the same box may also appear under **Fleet remote nodes** above, holding a
 * `socat` on a serial device. Those are two different statements about it and
 * this card makes neither on the other's behalf: a collector being logged in
 * says nothing about whether a console can be opened on it.
 *
 * **The password is typed here and kept nowhere.** The backend holds it in
 * memory for the life of its process and writes it to no file, so a restart
 * leaves the collector registered and unable to log in — which is what
 * `has_password` is for. The row asks for it again rather than showing a
 * Connect button that could only fail.
 */
export default function EdgeCollectorsCard() {
  const { role } = useAuth();
  const isAdmin = role === "admin";
  const [collectors, setCollectors] = useState<CollectorStatus[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!isAdmin) return;
    try {
      setCollectors(await getCollectors());
    } catch (err) {
      setError(humanizeApiError(err));
    }
  }, [isAdmin]);

  useEffect(() => {
    void refresh();
    // Polled, and polled unconditionally while this card is on screen. The
    // light below claims a session is being held right now, and the only thing
    // that knows a session died is the backend's process table — nothing pushes
    // that. Five seconds is slow enough to be free on a settings page and fast
    // enough that a dropped collector does not sit there breathing green.
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (!isAdmin) {
    return null;
  }

  const act = async (id: string, run: () => Promise<unknown>, done: string) => {
    setBusyId(id);
    setError(null);
    setNotice(null);
    try {
      await run();
      setNotice(done);
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusyId(null);
      await refresh();
    }
  };

  const connect = (collector: CollectorStatus) =>
    act(
      collector.id,
      async () => {
        const result = await connectCollector(collector.id);
        if (!result.hostname_matches) {
          // Not thrown: the login worked. Said out loud anyway, because the
          // address answered to a name nobody registered, and a log collector
          // being a different box than the operator thinks is the failure this
          // field exists to catch.
          setNotice(
            `Connected, but ${collector.ip} calls itself "${result.reported_hostname}", ` +
              `not "${collector.hostname}".`,
          );
        }
      },
      `${collector.label} connected.`,
    );

  return (
    <Card
      title="Edge log collectors"
      subtitle="Boxes reached over SSH — the Pi itself, and the DUT consoles behind it"
    >
      <div className="settings-list">
        <div className="setting-hint">
          {collectors.length} of {MAX_COLLECTORS} registered. Passwords are held{" "}
          <strong>in memory only</strong> — nothing writes them to disk, so a backend restart
          keeps the collector and asks for the login again. SSH to a new collector by hand
          once first: an unknown host key is reported here, never accepted for you.
        </div>

        {collectors.length === 0 ? (
          <div className="setting-hint">Nothing registered yet.</div>
        ) : (
          <ul className="collector-list">
            {collectors.map((collector) => (
              <CollectorRow
                key={collector.id}
                collector={collector}
                busy={busyId === collector.id}
                onConnect={() => connect(collector)}
                onDisconnect={() =>
                  act(
                    collector.id,
                    () => disconnectCollector(collector.id),
                    `${collector.label} disconnected.`,
                  )
                }
                onPassword={(password) =>
                  act(
                    collector.id,
                    () => setCollectorPassword(collector.id, password),
                    `Password held for ${collector.label} until the backend restarts.`,
                  )
                }
                onForget={() => {
                  if (
                    !window.confirm(
                      `Remove ${collector.label}? Its SSH session is closed and the ` +
                        "registration is dropped. Nothing on the collector is touched.",
                    )
                  ) {
                    return;
                  }
                  void act(
                    collector.id,
                    () => removeCollector(collector.id),
                    `${collector.label} removed.`,
                  );
                }}
              />
            ))}
          </ul>
        )}

        <RegisterForm
          full={collectors.length >= MAX_COLLECTORS}
          known={(id) => collectors.some((c) => c.id === id)}
          onDone={async (message) => {
            setError(null);
            setNotice(message);
            await refresh();
          }}
          onError={(message) => {
            setNotice(null);
            setError(message);
          }}
        />

        {error ? <div className="flash">{error}</div> : null}
        {notice ? <div className="setting-hint">{notice}</div> : null}
      </div>
    </Card>
  );
}

function CollectorRow({
  collector,
  busy,
  onConnect,
  onDisconnect,
  onPassword,
  onForget,
}: {
  collector: CollectorStatus;
  busy: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  onPassword: (password: string) => void;
  onForget: () => void;
}) {
  const [password, setPassword] = useState("");

  return (
    <li className="collector-row">
      <div className="collector-identity">
        <div className="collector-name">
          {/* The dot repeats the word beside it on purpose. The word is what a
              screen reader gets and what survives `prefers-reduced-motion`;
              the motion is what an eye catches from across the room, and it is
              the only part of this row that stops when the session does. */}
          <LiveDot live={collector.connected} />
          <span>{collector.label}</span>
          <span className={collector.connected ? "fleet-fact-ok" : "fleet-fact-idle"}>
            {collector.connected ? "Connected" : "Not connected"}
          </span>
        </div>
        <div className="card-sub">
          {collector.user}@{collector.ip}:{collector.port}
          {/* Only when somebody recorded an expectation. A collector migrated
              from a remote node has none, and printing "expects null" would
              invent one. */}
          {collector.hostname ? (
            <>
              {" · expects "}
              <code>{collector.hostname}</code>
            </>
          ) : null}
          {collector.auth === "key" ? (
            <>
              {" · key "}
              <code>{collector.key_path}</code>
            </>
          ) : null}
          {collector.connected && collector.connected_since
            ? ` · since ${collector.connected_since}`
            : ""}
        </div>
        {collector.detail ? <div className="card-sub">{collector.detail}</div> : null}
      </div>

      {collector.connected ? <ConsolePanel collector={collector} /> : null}

      <div className="collector-actions">
        {/* `ready`, not `has_password`: a key collector needs no password and
            must not be shown a field asking for one. */}
        {collector.ready ? (
          collector.connected ? (
            <button type="button" className="btn" disabled={busy} onClick={onDisconnect}>
              Disconnect
            </button>
          ) : (
            <button type="button" className="btn" disabled={busy} onClick={onConnect}>
              {busy ? "Connecting…" : "Connect"}
            </button>
          )
        ) : (
          <form
            className="collector-password"
            onSubmit={(event) => {
              event.preventDefault();
              onPassword(password);
              setPassword("");
            }}
          >
            <input
              className="input"
              type="password"
              value={password}
              placeholder="SSH password"
              autoComplete="off"
              onChange={(event) => setPassword(event.target.value)}
              aria-label={`SSH password for ${collector.label}`}
            />
            <button type="submit" className="btn" disabled={busy || !password}>
              Hold password
            </button>
          </form>
        )}
        <button type="button" className="btn" disabled={busy} onClick={onForget}>
          Remove
        </button>
      </div>
    </li>
  );
}

type CollectorForm = {
  id: string;
  label: string;
  ip: string;
  hostname: string;
  user: string;
  auth: "password" | "key";
  password: string;
  keyPath: string;
  port: string;
};

const BLANK: CollectorForm = {
  id: "",
  label: "",
  ip: "",
  hostname: "",
  user: "",
  auth: "password",
  password: "",
  keyPath: "",
  port: "22",
};

function RegisterForm({
  full,
  known,
  onDone,
  onError,
}: {
  full: boolean;
  known: (id: string) => boolean;
  onDone: (message: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [form, setForm] = useState<CollectorForm>(BLANK);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const set = <K extends keyof CollectorForm>(key: K, value: CollectorForm[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  // Re-posting an existing id re-configures it, which stays allowed at the
  // limit; only a genuinely new one is blocked.
  const blocked = full && !known(form.id.trim());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    const id = form.id.trim();
    try {
      await configureCollector({
        id,
        label: form.label.trim() || undefined,
        ip: form.ip.trim(),
        // Optional: it is what the box is EXPECTED to call itself, checked
        // against what it answers. Nobody's expectation is a real state.
        hostname: form.hostname.trim() || null,
        user: form.user.trim(),
        port: Number(form.port) || 22,
        auth: form.auth,
        ...(form.auth === "key"
          ? { key_path: form.keyPath.trim() }
          : { password: form.password }),
      });
      if (mounted.current) {
        setForm(BLANK);
      }
      await onDone(`${id} registered. Press Connect to log in.`);
    } catch (err) {
      onError(humanizeApiError(err));
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  return (
    <form className="invite-form" onSubmit={submit}>
      <label className="modal-label">
        Collector id
        <input
          className="input"
          value={form.id}
          onChange={(e) => set("id", e.target.value)}
          placeholder="edge1"
          required
        />
      </label>
      <label className="modal-label">
        Label
        <input
          className="input"
          value={form.label}
          onChange={(e) => set("label", e.target.value)}
          placeholder="Edge collector (lab)"
          maxLength={48}
        />
      </label>
      <label className="modal-label">
        IP address
        <input
          className="input"
          value={form.ip}
          onChange={(e) => set("ip", e.target.value)}
          placeholder="192.168.30.124"
          required
        />
      </label>
      <label className="modal-label">
        Hostname (optional)
        <input
          className="input"
          value={form.hostname}
          onChange={(e) => set("hostname", e.target.value)}
          placeholder="edge-collector"
        />
      </label>
      <label className="modal-label">
        SSH user
        <input
          className="input"
          value={form.user}
          onChange={(e) => set("user", e.target.value)}
          placeholder="pi"
          required
        />
      </label>
      <label className="modal-label">
        Authentication
        <select
          className="input"
          value={form.auth}
          onChange={(e) => set("auth", e.target.value as CollectorForm["auth"])}
        >
          <option value="password">Password (held in memory)</option>
          <option value="key">SSH key (a file on this machine)</option>
        </select>
      </label>
      {form.auth === "password" ? (
        <label className="modal-label">
          SSH password
          <input
            className="input"
            type="password"
            value={form.password}
            onChange={(e) => set("password", e.target.value)}
            autoComplete="off"
            required
          />
        </label>
      ) : (
        <label className="modal-label">
          Private key path
          <input
            className="input"
            value={form.keyPath}
            onChange={(e) => set("keyPath", e.target.value)}
            placeholder="/home/you/.ssh/dut_fleet_ed25519"
            required
          />
        </label>
      )}
      <label className="modal-label">
        SSH port
        <input
          className="input"
          type="number"
          min={1}
          max={65535}
          value={form.port}
          onChange={(e) => set("port", e.target.value)}
        />
      </label>
      <div className="setting-hint">
        The address is what SSH dials — an IP or a name. The hostname is what the box should
        call itself; a disagreement is reported after the login rather than hidden, and
        leaving it blank simply means nobody is checking. A <strong>key</strong> must have no
        passphrase and its path is on <strong>this dashboard's machine</strong>; a{" "}
        <strong>password</strong> is held in memory only and a restart asks for it again.
      </div>
      <button type="submit" className="btn primary" disabled={busy || blocked}>
        {busy ? "Registering…" : "Register collector"}
      </button>
      {blocked ? (
        <div className="setting-hint">
          The table is full ({MAX_COLLECTORS}). Remove one, or re-use an existing id to
          re-configure it.
        </div>
      ) : null}
    </form>
  );
}

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
function ConsolePanel({ collector }: { collector: CollectorStatus }) {
  const [consoles, setConsoles] = useState<CollectorConsoles | null>(null);
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

