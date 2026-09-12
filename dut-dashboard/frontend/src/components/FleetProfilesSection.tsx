import { useCallback, useEffect, useState } from "react";

import {
  deleteFleetProfile,
  FleetProfile,
  FleetProfileInput,
  getFleetProfiles,
  humanizeApiError,
  ProfileScope,
  updateFleetProfile,
} from "../api/rest";
import { formatTimestamp } from "../utils/datetime";
import { Card, EmptyState } from "./shell/Card";

/**
 * Saved host settings — the Hosts form, kept.
 *
 * Everything here is a convenience: a profile configures nothing and reaches no
 * machine. It holds where a box is, which port, who to log in as and what to
 * call it, and **never a password** — those live in the backend's memory for
 * the life of its process and are written to no file, which a saved row must
 * not be allowed to undo.
 *
 * Two columns are the whole social contract of the page. *Scope* says who else
 * sees the row: shared is the bench, private is its author alone. *Owner* says
 * who may change it — and that is always only the owner, shared or not, so a
 * setting somebody offers the bench cannot be rewritten under them. The backend
 * decides both; `can_edit` is its answer, not this page's guess.
 */
export default function FleetProfilesSection() {
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setProfiles(await getFleetProfiles());
      setError(null);
    } catch (err) {
      setError(humanizeApiError(err));
    }
  }, []);

  useEffect(() => {
    // Once. Nothing here changes unless somebody on this bench presses Save,
    // and a table of saved text is not worth a poll.
    void refresh();
  }, [refresh]);

  const act = async (run: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await run();
      setNotice(done);
      await refresh();
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title="Saved fleet host settings"
      subtitle="Address, port and login name — never a password"
      actions={
        <button
          type="button"
          className="btn icon-btn"
          disabled={busy}
          onClick={() => void refresh()}
          title="Reload from the server"
          aria-label="Reload profiles"
        >
          <span aria-hidden>⟳</span>
        </button>
      }
    >
      {profiles.length === 0 ? (
        <EmptyState
          icon="▥"
          message="No profiles saved yet."
          hint="Save one from a card on the Hosts page — the 💾 button beside its fields."
        />
      ) : (
        <div className="profiles-table-wrap">
          <table className="filetable profiles-table">
            <thead>
              <tr>
                <th>Profile</th>
                <th>Device name</th>
                <th>Host</th>
                <th>Port</th>
                <th>Username</th>
                <th>Scope</th>
                <th>Owner</th>
                <th>Updated</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {profiles.map((profile) =>
                editing === profile.id ? (
                  <EditRow
                    key={profile.id}
                    profile={profile}
                    busy={busy}
                    onCancel={() => setEditing(null)}
                    onSave={async (input) => {
                      await act(
                        () => updateFleetProfile(profile.id, input),
                        `Saved "${input.name}".`,
                      );
                      setEditing(null);
                    }}
                  />
                ) : (
                  <tr key={profile.id}>
                    <td>
                      <strong>{profile.name}</strong>
                    </td>
                    <td>{profile.device_name || "—"}</td>
                    <td>{profile.host}</td>
                    <td>{profile.port}</td>
                    <td>{profile.username}</td>
                    <td>
                      <span
                        className={`pill ${profile.scope === "shared" ? "ok" : "idle"}`}
                      >
                        {profile.scope === "shared" ? "Shared" : "Private"}
                      </span>
                    </td>
                    <td>{profile.owner}</td>
                    <td>{formatTimestamp(profile.updated_at)}</td>
                    <td className="profiles-actions">
                      {/* Absent, not disabled, for somebody else's row: a
                          greyed pencil invites a click that can only 403, and
                          the reason it would is a fact about ownership that the
                          Owner column already states. */}
                      {profile.can_edit ? (
                        <>
                          <button
                            type="button"
                            className="btn icon-btn"
                            disabled={busy}
                            onClick={() => setEditing(profile.id)}
                            title={`Edit ${profile.name}`}
                            aria-label={`Edit ${profile.name}`}
                          >
                            <span aria-hidden>✎</span>
                          </button>
                          <button
                            type="button"
                            className="btn icon-btn danger"
                            disabled={busy}
                            onClick={() => {
                              if (
                                !window.confirm(
                                  `Delete the profile "${profile.name}"? Hosts registered ` +
                                    "from it are not touched.",
                                )
                              ) {
                                return;
                              }
                              void act(
                                () => deleteFleetProfile(profile.id),
                                `Deleted "${profile.name}".`,
                              );
                            }}
                            title={`Delete ${profile.name}`}
                            aria-label={`Delete ${profile.name}`}
                          >
                            <span aria-hidden>🗑</span>
                          </button>
                        </>
                      ) : (
                        <span className="card-sub">{profile.owner}&rsquo;s</span>
                      )}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}

      {error ? <div className="flash">{error}</div> : null}
      {notice ? <div className="setting-hint">{notice}</div> : null}
    </Card>
  );
}

/** One row, in edit mode. The same nine columns, so nothing moves under the eye. */
function EditRow({
  profile,
  busy,
  onCancel,
  onSave,
}: {
  profile: FleetProfile;
  busy: boolean;
  onCancel: () => void;
  onSave: (input: FleetProfileInput) => Promise<void>;
}) {
  const [form, setForm] = useState({
    name: profile.name,
    device_name: profile.device_name ?? "",
    host: profile.host,
    port: String(profile.port),
    username: profile.username,
    scope: profile.scope,
  });

  const set = (key: keyof typeof form, value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  const submit = () =>
    void onSave({
      name: form.name.trim(),
      device_name: form.device_name.trim() || null,
      host: form.host.trim(),
      port: Number(form.port) || 22,
      username: form.username.trim(),
      scope: form.scope,
    });

  return (
    <tr className="profiles-editing">
      <td>
        <input
          className="input"
          value={form.name}
          onChange={(event) => set("name", event.target.value)}
          aria-label="Profile name"
          maxLength={48}
        />
      </td>
      <td>
        <input
          className="input"
          value={form.device_name}
          onChange={(event) => set("device_name", event.target.value)}
          aria-label="Device name"
          maxLength={48}
        />
      </td>
      <td>
        <input
          className="input"
          value={form.host}
          onChange={(event) => set("host", event.target.value)}
          aria-label="Host"
        />
      </td>
      <td>
        <input
          className="input"
          type="number"
          min={1}
          max={65535}
          value={form.port}
          onChange={(event) => set("port", event.target.value)}
          aria-label="Port"
        />
      </td>
      <td>
        <input
          className="input"
          value={form.username}
          onChange={(event) => set("username", event.target.value)}
          aria-label="Username"
        />
      </td>
      <td>
        <select
          className="input"
          value={form.scope}
          onChange={(event) => set("scope", event.target.value as ProfileScope)}
          aria-label="Scope"
        >
          <option value="private">Private</option>
          <option value="shared">Shared</option>
        </select>
      </td>
      <td>{profile.owner}</td>
      <td>{formatTimestamp(profile.updated_at)}</td>
      <td className="profiles-actions">
        <button
          type="button"
          className="btn primary"
          disabled={busy || !form.name.trim()}
          onClick={submit}
        >
          Save
        </button>
        <button type="button" className="btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </td>
    </tr>
  );
}
