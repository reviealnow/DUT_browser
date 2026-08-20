import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  configureRemoteNode,
  createInvite,
  CreatedInvite,
  DutInfo,
  getDuts,
  humanizeApiError,
  InviteSummary,
  listInvites,
  listRoleChanges,
  listUsers,
  MAX_REMOTE_NODES,
  removeDut,
  revokeInvite,
  Role,
  RoleChange,
  UserRecord,
} from "../api/rest";
import { DEFAULT_DUT_ID } from "../api/dut";
import { useAuth } from "../monitoring/AuthContext";
import { useCrashKeywords } from "../monitoring/useCrashKeywords";
import { ACCENT_PRESETS, useSettings } from "../monitoring/useSettings";
import { copyToClipboard } from "../utils/clipboard";
import { Card } from "./shell/Card";

const BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];

export default function SettingsSection({
  selectedDut,
  onSelectDut,
  onRegistryChanged,
}: {
  /** Removing the DUT the app is currently showing has to move the selection
   *  off it, exactly as the Topbar switcher does — otherwise every section goes
   *  on asking the backend about an id that no longer exists. */
  selectedDut: string;
  onSelectDut: (dutId: string) => void;
  /** Tells the shell the DUT registry moved, so the topbar switcher re-reads it. */
  onRegistryChanged: () => void;
}) {
  const { settings, setAccent, setDefaultBaud, setDisplayName } = useSettings();
  const { keywords, saving, saveKeywords } = useCrashKeywords();
  const [kwInput, setKwInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function addKeyword() {
    const kw = kwInput.trim();
    if (!kw) return;
    if (keywords.some((k) => k.toLowerCase() === kw.toLowerCase())) {
      setKwInput("");
      return;
    }
    void saveKeywords([...keywords, kw]);
    setKwInput("");
    inputRef.current?.focus();
  }

  function removeKeyword(kw: string) {
    void saveKeywords(keywords.filter((k) => k.toLowerCase() !== kw.toLowerCase()));
  }

  return (
    <>
      <Card title="Settings" subtitle="Saved in this browser">
        <div className="settings-list">
          <div className="setting-row">
            <div>
              <div className="setting-label">Display name</div>
              <div className="setting-hint">
                Tagged as the uploader / author in the Files and Bulletin workspace. Optional — left blank shows "—".
              </div>
            </div>
            <input
              type="text"
              value={settings.displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. nelson"
              aria-label="Display name"
              maxLength={40}
            />
          </div>

          <div className="setting-row">
            <div>
              <div className="setting-label">Accent colour</div>
              <div className="setting-hint">Theme accent for the whole dashboard.</div>
            </div>
            <div className="accent-swatches">
              {ACCENT_PRESETS.map((preset) => (
                <button
                  key={preset.accent}
                  type="button"
                  className={`accent-swatch${preset.accent === settings.accent ? " active" : ""}`}
                  style={{ background: preset.accent }}
                  title={preset.name}
                  aria-label={preset.name}
                  aria-pressed={preset.accent === settings.accent}
                  onClick={() => setAccent(preset.accent)}
                />
              ))}
            </div>
          </div>

          <div className="setting-row">
            <div>
              <div className="setting-label">Default baud rate</div>
              <div className="setting-hint">Pre-fills the serial connection form.</div>
            </div>
            <select
              value={settings.defaultBaud}
              onChange={(e) => setDefaultBaud(Number(e.target.value))}
              aria-label="Default baud rate"
            >
              {BAUD_RATES.map((rate) => (
                <option key={rate} value={rate}>
                  {rate}
                </option>
              ))}
            </select>
          </div>
        </div>
      </Card>

      <Card title="Crash Keywords" subtitle="Persisted on the server · used by all monitors">
        <div className="settings-list">
          <div className="setting-hint" style={{ marginBottom: 8 }}>
            Any log line matching one of these keywords increments the Critical Crash counter and appears
            in the crash panel. An empty list disables crash detection.
          </div>
          <div className="kw-chips">
            {keywords.length === 0 && (
              <span className="setting-hint" style={{ fontStyle: "italic" }}>
                No keywords — crash detection disabled.
              </span>
            )}
            {keywords.map((kw) => (
              <span key={kw} className="kw-chip">
                {kw}
                <button
                  type="button"
                  className="kw-chip-remove"
                  aria-label={`Remove "${kw}"`}
                  disabled={saving}
                  onClick={() => removeKeyword(kw)}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
          <div className="kw-add-row">
            <input
              ref={inputRef}
              type="text"
              value={kwInput}
              onChange={(e) => setKwInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addKeyword();
                }
              }}
              placeholder="Add keyword…"
              aria-label="New crash keyword"
              maxLength={120}
              disabled={saving}
            />
            <button type="button" onClick={addKeyword} disabled={saving || !kwInput.trim()}>
              Add
            </button>
          </div>
        </div>
      </Card>
      <RemoteNodesCard
        selectedDut={selectedDut}
        onSelectDut={onSelectDut}
        onRegistryChanged={onRegistryChanged}
      />
      <InvitesCard />
      <UsersCard />
    </>
  );
}

type NodeForm = {
  id: string;
  label: string;
  host: string;
  user: string;
  keyPath: string;
  port: string;
  device: string;
  baudrate: string;
  isMesh: boolean;
  backhaulIface: string;
};

const BLANK_NODE: NodeForm = {
  id: "",
  label: "",
  host: "",
  user: "",
  keyPath: "",
  port: "22",
  device: "/dev/ttyUSB0",
  baudrate: "115200",
  isMesh: true,
  backhaulIface: "",
};

/**
 * Admin-only registration for SSH-backed remote nodes.
 *
 * The strip on Overview could already connect, disconnect and capture, but
 * nothing in the frontend called `POST /api/fleet/nodes`: registering a node
 * meant a `curl` with a session cookie copied out of the browser, which is what
 * `docs/fleet-remote-nodes.md` had to tell people to do.
 *
 * In Settings rather than on the strip, because the strip hides itself when the
 * fleet has one DUT or fewer — precisely the state you are in before the first
 * node exists, so an "Add node" control there could never add the first one.
 *
 * What it shows about a registered node is what `/api/duts` returns: host, port
 * and device. `user` and `key_path` are deliberately not in that response and
 * are not echoed back here — the key never leaves the dashboard's machine.
 */
function RemoteNodesCard({
  selectedDut,
  onSelectDut,
  onRegistryChanged,
}: {
  selectedDut: string;
  onSelectDut: (dutId: string) => void;
  onRegistryChanged: () => void;
}) {
  const { role } = useAuth();
  const isAdmin = role === "admin";
  const [nodes, setNodes] = useState<DutInfo[]>([]);
  const [form, setForm] = useState<NodeForm>(BLANK_NODE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (!isAdmin) {
      return;
    }
    getDuts()
      .then((duts) => setNodes(duts.filter((dut) => dut.remote !== null)))
      .catch((err) => setError(humanizeApiError(err)));
  }, [isAdmin]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Settings is an engineer section and all four fleet routes are admin-only,
  // so an engineer gets nothing here rather than a form that can only 403.
  if (!isAdmin) {
    return null;
  }

  const set = <K extends keyof NodeForm>(key: K, value: NodeForm[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  // Re-posting an existing id re-configures that node, which stays allowed at
  // the limit; only a new one is blocked. Trimmed on both sides of the compare
  // because the id is trimmed before it is sent.
  const known = nodes.some((node) => node.id === form.id.trim());
  const full = nodes.length >= MAX_REMOTE_NODES && !known;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    const id = form.id.trim();
    try {
      await configureRemoteNode({
        id,
        label: form.label.trim() || undefined,
        host: form.host.trim(),
        user: form.user.trim(),
        key_path: form.keyPath.trim(),
        port: Number(form.port) || 22,
        device: form.device.trim(),
        baudrate: Number(form.baudrate) || 115200,
        is_mesh: form.isMesh,
        // A non-mesh node has no backhaul to name, and sending a stale one
        // would persist a value the card then reports as configured.
        backhaul_iface: form.isMesh ? form.backhaulIface.trim() || null : null,
      });
      setForm(BLANK_NODE);
      setNotice(
        `${id} registered. Connect it from the Fleet strip on Overview — ` +
          "nodes before roots, so a root can be named from its child.",
      );
      refresh();
      onRegistryChanged();
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const forget = async (node: DutInfo) => {
    if (
      !window.confirm(
        `Remove ${node.label || node.id}? Its console session is closed and the ` +
          "registration is dropped. Nothing on the Pi is touched.",
      )
    ) {
      return;
    }
    setError(null);
    setNotice(null);
    try {
      await removeDut(node.id);
      if (node.id === selectedDut) {
        onSelectDut(DEFAULT_DUT_ID);
      }
      refresh();
      onRegistryChanged();
    } catch (err) {
      setError(humanizeApiError(err));
    }
  };

  return (
    <Card
      title="Fleet remote nodes"
      subtitle="DUTs reached over SSH to a Raspberry Pi running socat"
    >
      <div className="settings-list">
        <div className="setting-hint">
          {nodes.length} of {MAX_REMOTE_NODES} registered. The key lives on{" "}
          <strong>this dashboard's machine</strong> and must have no passphrase — the backend
          runs <code>ssh -o BatchMode=yes</code> with nobody to type one. Connect to the Pi by
          hand once first, or every attempt fails on <code>Host key verification failed</code>.
        </div>

        <form className="invite-form" onSubmit={submit}>
          <label className="modal-label">
            DUT id
            <input
              className="input"
              value={form.id}
              onChange={(e) => set("id", e.target.value)}
              placeholder="node1"
              required
            />
          </label>
          <label className="modal-label">
            Label
            <input
              className="input"
              value={form.label}
              onChange={(e) => set("label", e.target.value)}
              placeholder="Mesh Node (420)"
              maxLength={80}
            />
          </label>
          <label className="modal-label">
            Pi host
            <input
              className="input"
              value={form.host}
              onChange={(e) => set("host", e.target.value)}
              placeholder="192.168.30.124"
              required
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
          <label className="modal-label">
            Serial device on the Pi
            <input
              className="input"
              value={form.device}
              onChange={(e) => set("device", e.target.value)}
              placeholder="/dev/ttyUSB0"
              required
            />
          </label>
          <label className="modal-label">
            Baud rate
            <select
              className="input"
              value={form.baudrate}
              onChange={(e) => set("baudrate", e.target.value)}
            >
              {BAUD_RATES.map((rate) => (
                <option key={rate} value={rate}>
                  {rate}
                </option>
              ))}
            </select>
          </label>
          <label className="modal-label node-mesh-toggle">
            <span>Mesh node</span>
            <input
              type="checkbox"
              checked={form.isMesh}
              onChange={(e) => set("isMesh", e.target.checked)}
            />
          </label>
          {form.isMesh ? (
            <label className="modal-label">
              Backhaul interface
              <input
                className="input"
                value={form.backhaulIface}
                onChange={(e) => set("backhaulIface", e.target.value)}
                placeholder="ath16"
                required
              />
            </label>
          ) : null}
          <button type="submit" className="btn primary" disabled={busy || full}>
            {busy ? "Saving…" : known ? "Update node" : "Register node"}
          </button>
        </form>

        <div className="setting-hint">
          The interface is a <strong>fallback</strong>: detection overrides it wherever it works,
          and it is what a root falls back to, since a root cannot name its own backhaul VAP from
          its own console. Clear <em>Mesh node</em> for a standalone AP — the link rows then read{" "}
          <code>Not applicable</code> instead of <code>Not captured</code>.
        </div>

        {full ? (
          <div className="setting-hint">
            The limit of {MAX_REMOTE_NODES} remote nodes is reached — remove one to add another.
            Re-registering an id already in the table is still allowed.
          </div>
        ) : null}
        {error ? <div className="flash">{error}</div> : null}
        {notice ? <div className="setting-hint">{notice}</div> : null}

        {nodes.length === 0 ? (
          <div className="setting-hint">No remote nodes registered.</div>
        ) : (
          <div className="invite-table-wrap">
            <table className="filetable invite-table">
              <thead>
                <tr>
                  <th>DUT</th>
                  <th>CONSOLE</th>
                  <th>MESH</th>
                  <th>SESSION</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {nodes.map((node) => (
                  <tr key={node.id}>
                    <td>
                      {node.label}
                      <div className="setting-hint">{node.id}</div>
                    </td>
                    <td>
                      {node.remote!.host}:{node.remote!.port}
                      <div className="setting-hint">{node.remote!.device}</div>
                    </td>
                    <td>
                      {/* `role` is null until a capture has run: an absent uplink is
                          an answer, an absent capture is not. */}
                      {node.remote!.is_mesh ? node.remote!.role ?? "not captured" : "standalone"}
                    </td>
                    <td>{node.serial_open ? "open" : "closed"}</td>
                    <td>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void forget(node)}
                        disabled={!node.removable}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

/**
 * Admin-only audit view. `users.role` only ever holds the current value, so the
 * role-change log below it is the only record of how someone got their
 * privileges — that is why both are shown together.
 */
function UsersCard() {
  const { role } = useAuth();
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [changes, setChanges] = useState<RoleChange[]>([]);
  const [error, setError] = useState<string | null>(null);
  const isAdmin = role === "admin";

  useEffect(() => {
    if (!isAdmin) {
      return;
    }
    Promise.all([listUsers(), listRoleChanges(50)])
      .then(([u, c]) => {
        setUsers(u);
        setChanges(c);
      })
      .catch((err) => setError(humanizeApiError(err)));
  }, [isAdmin]);

  if (!isAdmin) {
    return null;
  }

  return (
    <Card title="Users" subtitle="Who has registered, and how they got their role">
      <div className="settings-list">
        {error ? <div className="flash">{error}</div> : null}
        <div className="invite-table-wrap">
          <table className="filetable invite-table">
            <thead>
              <tr>
                <th>USER</th>
                <th>ROLE</th>
                <th>REGISTERED</th>
                <th>LAST SEEN</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td>{user.display_name || user.username}</td>
                  <td>
                    <span className={`pill role-${user.role}`}>{user.role}</span>
                  </td>
                  <td>{user.created_at}</td>
                  <td>{user.last_seen_at ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="setting-label" style={{ marginTop: "var(--space-4)" }}>
          Role history
        </div>
        {changes.length === 0 ? (
          <div className="setting-hint">No role changes recorded yet.</div>
        ) : (
          <ul className="role-history">
            {changes.map((change) => (
              <li key={change.id}>
                <strong>{change.username}</strong>: {change.from_role ?? "new"} →{" "}
                {change.to_role} via {change.via}
                {change.invite_id !== null ? ` #${change.invite_id}` : ""} ·{" "}
                <span className="setting-hint">{change.changed_at}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

/**
 * Admin-only invite management. Settings itself is an engineer section, so this
 * card renders nothing for engineers — the backend refuses them anyway, this
 * just avoids showing a panel that could only ever return 403.
 */
function InvitesCard() {
  const { role } = useAuth();
  const [invites, setInvites] = useState<InviteSummary[]>([]);
  const [created, setCreated] = useState<CreatedInvite | null>(null);
  const [inviteRole, setInviteRole] = useState<Role>("engineer");
  const [label, setLabel] = useState("");
  const [expiryHours, setExpiryHours] = useState("168");
  const [maxUses, setMaxUses] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isAdmin = role === "admin";

  const refresh = useCallback(() => {
    if (!isAdmin) {
      return;
    }
    listInvites()
      .then(setInvites)
      .catch((err) => setError(humanizeApiError(err)));
  }, [isAdmin]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!isAdmin) {
    return null;
  }

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const hours = Number(expiryHours);
      const invite = await createInvite({
        role: inviteRole,
        label: label.trim() || undefined,
        // 0 means "never expires"; the backend takes null for that.
        expires_in_hours: hours > 0 ? hours : null,
        max_uses: Math.max(1, Number(maxUses) || 1),
      });
      setCreated(invite);
      setLabel("");
      refresh();
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: number) => {
    setError(null);
    try {
      await revokeInvite(id);
      if (created?.id === id) {
        setCreated(null);
      }
      refresh();
    } catch (err) {
      setError(humanizeApiError(err));
    }
  };

  const inviteUrl = created ? window.location.origin + created.url_path : "";

  return (
    <Card title="Invites" subtitle="Share a scannable link that grants a role without the passcode">
      <div className="settings-list">
        <form className="invite-form" onSubmit={create}>
          <label className="modal-label">
            Role
            <select
              className="input"
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value as Role)}
            >
              <option value="guest">Guest</option>
              <option value="engineer">Engineer</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <label className="modal-label">
            Label
            <input
              className="input"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Who is it for?"
              maxLength={80}
            />
          </label>
          <label className="modal-label">
            Expires (hours, 0 = never)
            <input
              className="input"
              type="number"
              min={0}
              value={expiryHours}
              onChange={(e) => setExpiryHours(e.target.value)}
            />
          </label>
          <label className="modal-label">
            Max uses
            <input
              className="input"
              type="number"
              min={1}
              value={maxUses}
              onChange={(e) => setMaxUses(e.target.value)}
            />
          </label>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? "Creating…" : "Create invite"}
          </button>
        </form>

        {error ? <div className="flash">{error}</div> : null}

        {created ? (
          <div className="invite-result">
            <div className="setting-label">
              Invite ready — copy it now, it is shown only once
            </div>
            <div className="setting-hint">
              Grants <strong>{created.role}</strong>
              {created.expires_at ? ` · expires ${created.expires_at} UTC` : " · never expires"} ·{" "}
              {created.max_uses === 1 ? "single use" : `${created.max_uses} uses`}
            </div>
            {created.qr_svg ? (
              <img
                className="invite-qr"
                // Data URL rather than dangerouslySetInnerHTML: the SVG is
                // rendered as an image, never parsed into this document.
                src={`data:image/svg+xml;utf8,${encodeURIComponent(created.qr_svg)}`}
                alt={`QR code for the ${created.role} invite`}
              />
            ) : null}
            <code className="invite-url">{inviteUrl}</code>
            <button
              type="button"
              className="btn"
              onClick={() => void copyToClipboard(inviteUrl)}
            >
              Copy link
            </button>
          </div>
        ) : null}

        {invites.length === 0 ? (
          <div className="setting-hint">No invites yet.</div>
        ) : (
          <div className="invite-table-wrap">
          <table className="filetable invite-table">
            <thead>
              <tr>
                <th>ROLE</th>
                <th>LABEL</th>
                <th>USES</th>
                <th>STATUS</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {invites.map((invite) => {
                const status = invite.revoked
                  ? "revoked"
                  : invite.exhausted
                    ? "used up"
                    : "active";
                return (
                  <tr key={invite.id}>
                    <td>{invite.role}</td>
                    <td>{invite.label ?? "—"}</td>
                    <td>
                      {invite.used_count} / {invite.max_uses}
                    </td>
                    <td>{status}</td>
                    <td>
                      {status === "active" ? (
                        <button type="button" className="btn" onClick={() => void revoke(invite.id)}>
                          Revoke
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </Card>
  );
}
