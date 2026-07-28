import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  createInvite,
  CreatedInvite,
  humanizeApiError,
  InviteSummary,
  listInvites,
  revokeInvite,
  Role,
} from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";
import { useCrashKeywords } from "../monitoring/useCrashKeywords";
import { ACCENT_PRESETS, useSettings } from "../monitoring/useSettings";
import { Card } from "./shell/Card";

const BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];

export default function SettingsSection() {
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
      <InvitesCard />
    </>
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
              onClick={() => void navigator.clipboard?.writeText(inviteUrl)}
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
