import { useCallback, useEffect, useRef, useState } from "react";

import {
  CollectorStatus,
  getCollectorHostKey,
  HostKeyStatus,
  trustCollectorHostKey,
  configureCollector,
  connectCollector,
  disconnectCollector,
  FleetProfile,
  getCollectors,
  getFleetProfiles,
  humanizeApiError,
  MAX_COLLECTORS,
  ProfileScope,
  createFleetProfile,
  removeCollector,
} from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";
import CollectorConsoles from "./CollectorConsoles";
import { EmptyState } from "./shell/Card";
import LiveDot from "./shell/LiveDot";

/**
 * Every box this dashboard can reach over SSH, one card each.
 *
 * A host is the Raspberry Pi as a machine, not as somebody's console. It sits
 * in the middle of this bench:
 *
 *   LAN DUT console  <<Server>>  Raspberry Pi  (ssh)  >>  LAN DUT console
 *
 * so a card here has two halves: the login at the top, and — once the session
 * is up — the DUT consoles behind it underneath.
 *
 * **The password is typed here and kept nowhere.** The backend holds it in
 * memory for the life of its process and writes it to no file, so a restart
 * leaves the host registered and unable to log in. That is what the footer says
 * in words, rather than offering a Verify that could only fail.
 *
 * The card is also the editor. There is no separate registration form: a new
 * host is an empty card, an existing one is the same card with its fields
 * filled, and Verify writes whatever is in them before logging in. The two
 * used to be different shapes for the same six fields, and the form was the
 * only way to correct a typo in an address.
 *
 * **Verify is not a dry run.** It writes the fields and opens the session that
 * the light at the top then reports; Disconnect is what closes it again. The
 * word is the one the bench uses for this button, and the state beside it is
 * what keeps it from reading as a test that changes nothing.
 */
export default function FleetHostsSection({
  onManageProfiles,
  onRegistryChanged,
}: {
  /** Takes the reader to Fleet > Profiles, where saved settings are edited. */
  onManageProfiles: () => void;
  /** Attaching a console registers a DUT; removing a host closes the sessions
   *  behind it. Both change the registry the topbar switcher lists, and it
   *  reads that once plus whenever this fires. */
  onRegistryChanged: () => void;
}) {
  const { role } = useAuth();
  const isAdmin = role === "admin";
  const [collectors, setCollectors] = useState<CollectorStatus[]>([]);
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [drafts, setDrafts] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const nextDraft = useRef(1);

  const refresh = useCallback(async () => {
    if (!isAdmin) return;
    try {
      setCollectors(await getCollectors());
    } catch (err) {
      setError(humanizeApiError(err));
    }
  }, [isAdmin]);

  const refreshProfiles = useCallback(async () => {
    if (!isAdmin) return;
    try {
      setProfiles(await getFleetProfiles());
    } catch (err) {
      // Not fatal and not shouted about: profiles only fill fields in. A host
      // page that refuses to draw because a convenience failed would be worse
      // than one whose Source list is short.
      setProfiles([]);
    }
  }, [isAdmin]);

  useEffect(() => {
    void refresh();
    // Polled, and polled unconditionally while this page is on screen. The
    // light below claims a session is being held right now, and the only thing
    // that knows a session died is the backend's process table — nothing pushes
    // that. Five seconds is slow enough to be free here and fast enough that a
    // dropped host does not sit there breathing green.
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    void refreshProfiles();
  }, [refreshProfiles]);

  if (!isAdmin) {
    return null;
  }

  const full = collectors.length >= MAX_COLLECTORS;
  const ids = collectors.map((collector) => collector.id);

  return (
    <div className="hosts-page">
      <div className="hosts-head">
        <div className="setting-hint">
          Passwords are held <strong>in memory only</strong> — nothing writes them to disk, so
          a backend restart keeps the host and asks for the login again. SSH to a new host by
          hand once first: an unknown host key is reported here, never accepted for you.
        </div>
        <div className="hosts-head-actions">
          <span className="hosts-count" aria-label="Hosts registered">
            {collectors.length} / {MAX_COLLECTORS}
          </span>
          <button
            type="button"
            className="btn primary"
            disabled={full}
            title={full ? `The table is full (${MAX_COLLECTORS}). Remove one first.` : undefined}
            onClick={() => {
              setDrafts((current) => [...current, `draft-${nextDraft.current++}`]);
              setError(null);
              setNotice(null);
            }}
          >
            Add new host
          </button>
        </div>
      </div>

      {collectors.length === 0 && drafts.length === 0 ? (
        <EmptyState
          icon="🗄"
          message="Nothing registered yet."
          hint="Add a host to log into the Pi that has the DUT consoles on it."
        />
      ) : null}

      {collectors.map((collector) => (
        <HostCard
          key={collector.id}
          collector={collector}
          handle={collector.id}
          profiles={profiles}
          takenIds={ids}
          onSaved={async (message) => {
            setError(null);
            setNotice(message);
            await refresh();
          }}
          onProfileSaved={async (message) => {
            setError(null);
            setNotice(message);
            await refreshProfiles();
          }}
          onError={(message) => {
            setNotice(null);
            setError(message);
          }}
          onManageProfiles={onManageProfiles}
          onRegistryChanged={onRegistryChanged}
          onDrop={() => undefined}
        />
      ))}

      {drafts.map((key) => (
        <HostCard
          key={key}
          collector={null}
          handle={`new host ${key.replace("draft-", "")}`}
          profiles={profiles}
          takenIds={ids}
          onSaved={async (message) => {
            setError(null);
            setNotice(message);
            setDrafts((current) => current.filter((draft) => draft !== key));
            await refresh();
          }}
          onProfileSaved={async (message) => {
            setError(null);
            setNotice(message);
            await refreshProfiles();
          }}
          onError={(message) => {
            setNotice(null);
            setError(message);
          }}
          onManageProfiles={onManageProfiles}
          onRegistryChanged={onRegistryChanged}
          onDrop={() => setDrafts((current) => current.filter((draft) => draft !== key))}
        />
      ))}

      {error ? <div className="flash">{error}</div> : null}
      {notice ? <div className="setting-hint">{notice}</div> : null}
    </div>
  );
}

type HostForm = {
  label: string;
  host: string;
  port: string;
  username: string;
  password: string;
  hostname: string;
  auth: "password" | "key";
  keyPath: string;
};

const BLANK: HostForm = {
  label: "",
  host: "",
  port: "22",
  username: "",
  password: "",
  hostname: "",
  auth: "password",
  keyPath: "",
};

function formFor(collector: CollectorStatus | null): HostForm {
  if (collector === null) {
    return BLANK;
  }
  return {
    label: collector.label,
    host: collector.ip,
    port: String(collector.port),
    // Never filled in from the backend, which does not return it and could not.
    password: "",
    username: collector.user,
    hostname: collector.hostname ?? "",
    auth: collector.auth,
    keyPath: collector.key_path ?? "",
  };
}

/**
 * An id for a host nobody has named one.
 *
 * The registry keys everything on this id and re-posting one is an *edit*, so a
 * derived id that happens to collide would silently re-point somebody else's
 * registration at a different machine. `taken` is therefore every id already
 * registered, and a tie gets a suffix rather than the existing card. The shape
 * is `_ID_RE` in collector/registry.py.
 */
function deriveId(seed: string, taken: string[]): string {
  const base =
    seed
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 32) || "host";
  if (!taken.includes(base)) {
    return base;
  }
  for (let n = 2; n < 100; n += 1) {
    const candidate = `${base.slice(0, 29)}-${n}`;
    if (!taken.includes(candidate)) {
      return candidate;
    }
  }
  return base;
}

/**
 * The footer sentence: what this host's state means for the next press.
 *
 * Deliberately not a second copy of "Connected" -- the dot and the word beside
 * the name up top are what claim a live session. This line answers the other
 * question: whether pressing the button below can work at all.
 */
function statusOf(collector: CollectorStatus | null): string {
  if (collector === null) {
    return "Not registered yet";
  }
  if (collector.connected) {
    return collector.connected_since ? `Since ${collector.connected_since}` : "Session held";
  }
  if (!collector.ready) {
    // A restart forgot the password, by design. Said plainly, because the fix
    // is to type it again rather than to check the network.
    return "Needs its password again";
  }
  if (collector.detail) {
    // How the last attempt ended, as the backend's own sentence -- "The host
    // key is not known to this machine…", "The SSH session ended."
    //
    // It BECOMES the status rather than being appended to it. A failed login
    // leaves `ready` true, because the password is still held, so this line
    // used to read "Ready · The host key is not known to this machine", which
    // is two claims that contradict each other: the first says press the
    // button, the second says pressing it changes nothing until somebody
    // accepts a host key by hand. Reported from the bench.
    return collector.detail;
  }
  return "Ready";
}

function HostCard({
  collector,
  handle,
  onRegistryChanged,
  profiles,
  takenIds,
  onSaved,
  onProfileSaved,
  onError,
  onManageProfiles,
  onDrop,
}: {
  /** Null for a card that has never been registered. */
  collector: CollectorStatus | null;
  /** What this card is called in accessible names: its registered id, or which
   *  new card it is. The label cannot serve — two boxes may share one, and two
   *  fields answering to the same name is a form nobody can drive by keyboard. */
  handle: string;
  profiles: FleetProfile[];
  /** Every id the registry already holds, so a derived one cannot land on it. */
  takenIds: string[];
  onSaved: (message: string) => Promise<void>;
  onProfileSaved: (message: string) => Promise<void>;
  onError: (message: string) => void;
  onManageProfiles: () => void;
  onRegistryChanged: () => void;
  onDrop: () => void;
}) {
  // Initialised once per card and then owned by the card: the five-second poll
  // re-renders this component constantly, and re-deriving the fields from the
  // collector would delete whatever is being typed into them.
  const [form, setForm] = useState<HostForm>(() => formFor(collector));
  const [source, setSource] = useState("");
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const set = <K extends keyof HostForm>(key: K, value: HostForm[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const name = form.label || form.host || "this host";
  const connected = collector?.connected ?? false;
  // A password collector with nothing held has to be given one before a login
  // can be attempted; a key collector never does.
  const needsPassword =
    form.auth === "password" && !form.password && !(collector?.has_password ?? false);
  const incomplete = !form.host.trim() || !form.username.trim();
  const status = statusOf(collector);

  const applyProfile = (value: string) => {
    setSource(value);
    const profile = profiles.find((entry) => String(entry.id) === value);
    if (!profile) {
      return;
    }
    // Everything a profile holds, and nothing it does not: no password is
    // stored in one, so the field below is left exactly as the operator left it.
    setForm((current) => ({
      ...current,
      label: profile.device_name || current.label,
      host: profile.host,
      port: String(profile.port),
      username: profile.username,
    }));
  };

  const connect = async () => {
    setBusy(true);
    try {
      const id = collector?.id ?? deriveId(form.label || form.host, takenIds);
      await configureCollector({
        id,
        label: form.label.trim() || undefined,
        ip: form.host.trim(),
        // Preserved rather than re-declared: this is what the box is EXPECTED
        // to call itself, and a card that quietly cleared it would turn a real
        // mismatch into silence.
        hostname: form.hostname.trim() || null,
        user: form.username.trim(),
        port: Number(form.port) || 22,
        auth: form.auth,
        ...(form.auth === "key"
          ? { key_path: form.keyPath.trim() }
          : // Omitted when empty: absent means "keep whatever is held in
            // memory", which is what a re-connect of a working host needs.
            form.password
            ? { password: form.password }
            : {}),
      });
      const result = await connectCollector(id);
      setForm((current) => ({ ...current, password: "" }));
      await onSaved(
        result.hostname_matches
          ? `${form.label || id} connected.`
          : // Not an error: the login worked. Said out loud anyway, because the
            // address answered to a name nobody registered, and a log collector
            // being a different box than the operator thinks is the failure the
            // expected-hostname field exists to catch.
            `Connected, but ${form.host} calls itself "${result.reported_hostname}", not ` +
              `"${form.hostname}".`,
      );
    } catch (err) {
      onError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!collector) return;
    setBusy(true);
    try {
      await disconnectCollector(collector.id);
      await onSaved(`${collector.label} disconnected.`);
    } catch (err) {
      onError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!collector) {
      onDrop();
      return;
    }
    if (
      !window.confirm(
        `Remove ${collector.label}? Its SSH session is closed and the registration is ` +
          "dropped. Nothing on the host itself is touched.",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await removeCollector(collector.id);
      // Its consoles went with it; the switcher is listing DUTs whose session
      // this just ended.
      onRegistryChanged();
      await onSaved(`${collector.label} removed.`);
    } catch (err) {
      onError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card host-card">
      <div className="host-card-head">
        <LiveDot live={connected} />
        <input
          className="input host-name"
          value={form.label}
          placeholder="Host name"
          maxLength={48}
          onChange={(event) => set("label", event.target.value)}
          aria-label={`Name for ${handle}`}
        />
        <span className={connected ? "fleet-fact-ok" : "fleet-fact-idle"}>
          {connected ? "Connected" : "Not connected"}
        </span>
        <button
          type="button"
          className="btn icon-btn danger"
          disabled={busy}
          onClick={() => void remove()}
          title={collector ? `Remove ${collector.label}` : "Discard this card"}
          aria-label={collector ? `Remove ${collector.label}` : "Discard this card"}
        >
          <span aria-hidden>🗑</span>
        </button>
      </div>

      <div className="host-fields">
        <label className="modal-label">
          Source
          <select
            className="input"
            value={source}
            onChange={(event) => applyProfile(event.target.value)}
            aria-label={`Source for ${handle}`}
          >
            <option value="">User defined</option>
            {profiles.map((profile) => (
              <option key={profile.id} value={String(profile.id)}>
                {profile.name}
                {profile.scope === "private" ? " (private)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="modal-label">
          Host
          <input
            className="input"
            value={form.host}
            placeholder="192.168.30.124"
            onChange={(event) => set("host", event.target.value)}
            aria-label={`Address for ${handle}`}
          />
        </label>
        <label className="modal-label host-port">
          Port
          <input
            className="input"
            type="number"
            min={1}
            max={65535}
            value={form.port}
            onChange={(event) => set("port", event.target.value)}
            aria-label={`SSH port for ${handle}`}
          />
        </label>
        <label className="modal-label">
          Username
          <input
            className="input"
            value={form.username}
            placeholder="pi"
            onChange={(event) => set("username", event.target.value)}
            aria-label={`SSH user for ${handle}`}
          />
        </label>
        {form.auth === "password" ? (
          <label className="modal-label">
            Password
            <input
              className="input"
              type="password"
              value={form.password}
              autoComplete="off"
              placeholder={collector?.has_password ? "held in memory" : "required"}
              onChange={(event) => set("password", event.target.value)}
              aria-label={`SSH password for ${handle}`}
            />
          </label>
        ) : (
          <label className="modal-label">
            Key file
            <input
              className="input"
              value={form.keyPath}
              placeholder="/home/you/.ssh/dut_fleet_ed25519"
              onChange={(event) => set("keyPath", event.target.value)}
              aria-label={`Private key path for ${handle}`}
            />
          </label>
        )}
        <div className="host-field-actions">
          <button
            type="button"
            className="btn icon-btn"
            disabled={busy || incomplete}
            onClick={() => setSaving((current) => !current)}
            title="Save these fields as a profile"
            aria-label={`Save ${handle} as a profile`}
          >
            <span aria-hidden>💾</span>
          </button>
          <button
            type="button"
            className="btn icon-btn"
            onClick={onManageProfiles}
            title="Manage saved profiles"
            aria-label="Manage saved profiles"
          >
            <span aria-hidden>🗂</span>
          </button>
        </div>
      </div>

      {saving ? (
        <SaveAsProfile
          form={form}
          onCancel={() => setSaving(false)}
          onSaved={async (message) => {
            setSaving(false);
            await onProfileSaved(message);
          }}
          onError={onError}
        />
      ) : null}

      <button
        type="button"
        className="btn host-advanced-toggle"
        onClick={() => setAdvanced((current) => !current)}
        aria-expanded={advanced}
      >
        {advanced ? "Hide details" : "Details"}
      </button>

      {advanced ? (
        <div className="host-fields host-advanced">
          <label className="modal-label">
            Expected hostname
            <input
              className="input"
              value={form.hostname}
              placeholder="edge-collector"
              onChange={(event) => set("hostname", event.target.value)}
              aria-label={`Expected hostname for ${handle}`}
            />
          </label>
          <label className="modal-label">
            Authentication
            <select
              className="input"
              value={form.auth}
              onChange={(event) => set("auth", event.target.value as HostForm["auth"])}
              aria-label={`Authentication for ${handle}`}
            >
              <option value="password">Password (held in memory)</option>
              <option value="key">SSH key (a file on this machine)</option>
            </select>
          </label>
          <div className="setting-hint">
            The address is what SSH dials — an IP or a name. The expected hostname is what the
            box should call itself; a disagreement is reported after the login rather than
            hidden, and leaving it blank simply means nobody is checking. A{" "}
            <strong>key</strong> must have no passphrase and its path is on{" "}
            <strong>this dashboard's machine</strong>.
            {collector ? (
              <>
                {" "}
                Registered as <code>{collector.id}</code>.
              </>
            ) : null}
          </div>
          {/* Only for a host that exists: there is nothing to read a key from
              until an address has been registered. */}
          {collector ? (
            <HostKeyPanel
              collector={collector}
              onTrusted={onSaved}
              onError={onError}
            />
          ) : null}
        </div>
      ) : null}

      {connected && collector ? (
        <CollectorConsoles collector={collector} onRegistryChanged={onRegistryChanged} />
      ) : null}

      <div className="host-card-foot">
        <span className="host-status">
          {status}
          {/* Only when it is not already the status above -- which it is for a
              host with no session. A connected host's detail is the other kind:
              the login worked and the box answered to a name nobody registered,
              and that belongs beside "Since 11:02:33" rather than instead of it. */}
          {collector?.detail && collector.detail !== status ? (
            <span className="card-sub"> · {collector.detail}</span>
          ) : null}
        </span>
        {connected ? (
          <button type="button" className="btn" disabled={busy} onClick={() => void disconnect()}>
            Disconnect
          </button>
        ) : (
          <button
            type="button"
            className="btn primary"
            disabled={busy || incomplete || needsPassword}
            onClick={() => void connect()}
            title={
              needsPassword
                ? "Type the SSH password first — the backend holds none for this host."
                : undefined
            }
          >
            {busy ? "Verifying…" : "Verify"}
          </button>
        )}
      </div>
    </section>
  );
}

/**
 * Save what is in a card as a profile.
 *
 * Inline rather than a dialog because the fields it copies are on screen: the
 * only two things left to decide are what to call it and who else sees it.
 */
function SaveAsProfile({
  form,
  onCancel,
  onSaved,
  onError,
}: {
  form: HostForm;
  onCancel: () => void;
  onSaved: (message: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState(form.label || form.host);
  const [scope, setScope] = useState<ProfileScope>("private");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      await createFleetProfile({
        name: name.trim(),
        device_name: form.label.trim() || null,
        host: form.host.trim(),
        port: Number(form.port) || 22,
        username: form.username.trim(),
        scope,
      });
      await onSaved(
        `Saved "${name.trim()}"${scope === "shared" ? " for the bench" : ""}. ` +
          "No password was stored in it.",
      );
    } catch (err) {
      onError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="host-save-profile">
      <label className="modal-label">
        Profile name
        <input
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-label="Profile name"
          maxLength={48}
        />
      </label>
      <label className="modal-label">
        Scope
        <select
          className="input"
          value={scope}
          onChange={(event) => setScope(event.target.value as ProfileScope)}
          aria-label="Profile scope"
        >
          <option value="private">Private — only you</option>
          <option value="shared">Shared — the whole bench</option>
        </select>
      </label>
      <button
        type="button"
        className="btn primary"
        disabled={busy || !name.trim()}
        onClick={() => void save()}
      >
        {busy ? "Saving…" : "Save profile"}
      </button>
      <button type="button" className="btn" disabled={busy} onClick={onCancel}>
        Cancel
      </button>
      <div className="setting-hint">
        A profile holds the address, port, user and name — never the password.
      </div>
    </div>
  );
}

/**
 * The host key, where the operator already is.
 *
 * An unknown key is reported by this dashboard and never accepted for it, and
 * the way out used to be a terminal: SSH to the box by hand, read a fingerprint
 * nobody compares, type `yes`. That is the same decision this makes — it is
 * simply made here, with the fingerprint on screen and a line saying how to
 * check it for real.
 *
 * Trusting names a fingerprint. The backend re-reads the host and writes only
 * if it still matches, which is what separates this from
 * `StrictHostKeyChecking=accept-new`: the answer is about the key that was
 * shown, not about whatever answers next. A host that already has a key on
 * record is refused outright — a reimaged box and a replaced one look the same
 * from here.
 */
function HostKeyPanel({
  collector,
  onTrusted,
  onError,
}: {
  collector: CollectorStatus;
  onTrusted: (message: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [state, setState] = useState<HostKeyStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const read = useCallback(async () => {
    setBusy(true);
    try {
      setState(await getCollectorHostKey(collector.id));
    } catch (err) {
      onError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  }, [collector.id, onError]);

  useEffect(() => {
    void read();
  }, [read]);

  if (!state) {
    return <div className="setting-hint">{busy ? "Reading the host key…" : ""}</div>;
  }

  const presented = state.presented_keys[0] ?? null;
  return (
    <div className="hostkey">
      <div className="hostkey-line">
        <strong>Host key</strong>
        {state.known ? (
          state.matches === false ? (
            // The one state that means stop. Said as loudly as the layout allows.
            <span className="fleet-fact-danger">on record, and the host is presenting a different one</span>
          ) : state.matches === null ? (
            <span className="fleet-fact-idle">on record · nothing answered just now</span>
          ) : (
            <span className="fleet-fact-ok">on record, and it matches</span>
          )
        ) : (
          <span className="fleet-fact-idle">not known to this machine</span>
        )}
        <button type="button" className="btn" disabled={busy} onClick={() => void read()}>
          {busy ? "Reading…" : "Re-read"}
        </button>
      </div>

      {state.known_keys.map((key) => (
        <div key={`known-${key.fingerprint}`} className="hostkey-key">
          <span className="hostkey-what">on record</span>
          <code>{key.type}</code>
          <code className="hostkey-print">{key.fingerprint}</code>
        </div>
      ))}
      {state.presented_keys.map((key) => (
        <div key={`live-${key.fingerprint}`} className="hostkey-key">
          <span className="hostkey-what">presented</span>
          <code>{key.type}</code>
          <code className="hostkey-print">{key.fingerprint}</code>
        </div>
      ))}
      {state.scan_error ? <div className="setting-hint">{state.scan_error}</div> : null}

      {!state.known && presented ? (
        <>
          <div className="setting-hint">
            Nobody has verified this key. Trusting it here is the same answer as typing{" "}
            <code>yes</code> at an SSH prompt — and the same trust-on-first-use. To check it
            for real, read the fingerprint off the box itself:{" "}
            <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>, and compare.
          </div>
          <button
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void trustCollectorHostKey(collector.id, presented.fingerprint)
                .then(async (answer) => {
                  await onTrusted(
                    `Trusted ${answer.type} ${answer.trusted} for ${state.host}. Press Verify.`,
                  );
                  await read();
                })
                .catch((err) => onError(humanizeApiError(err)))
                .finally(() => setBusy(false));
            }}
          >
            Trust this key
          </button>
        </>
      ) : null}
      {state.known && state.matches === false ? (
        <div className="setting-hint">
          Nothing here will overwrite it. If the box really was reimaged, remove the old entry
          by hand — <code>ssh-keygen -R {state.host}</code> — and read this panel again.
        </div>
      ) : null}
    </div>
  );
}
