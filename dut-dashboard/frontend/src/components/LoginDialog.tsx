/**
 * Engineer/Admin login modal (P71b).
 *
 * Guests never see this: the landing model is guest-by-default, and this
 * dialog only exists to raise a browser's role. "Login" is really the backend's
 * register endpoint — identity is a claimed username plus the shared per-role
 * passcode, so first login and re-login are the same call.
 */
import { FormEvent, useEffect, useState } from "react";

import { getWhoami, humanizeApiError, Role } from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";

type Props = {
  onClose: () => void;
};

export default function LoginDialog({ onClose }: Props) {
  const { user, login } = useAuth();
  const [username, setUsername] = useState(user?.username ?? "");
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [role, setRole] = useState<Role>("engineer");
  const [passcode, setPasscode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Pre-fill the username from the caller's IP-derived suggestion, same as the
  // Workspace identity — only when the field is still empty, never overwriting
  // what the user typed.
  useEffect(() => {
    if (username) {
      return;
    }
    let cancelled = false;
    getWhoami()
      .then((who) => {
        if (!cancelled) {
          setUsername((current) => current || who.name);
        }
      })
      .catch(() => {
        // Suggestion only — an empty field is fine.
      });
    return () => {
      cancelled = true;
    };
  }, [username]);

  // Esc closes, matching the mobile nav drawer behaviour.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login({
        username: username.trim(),
        display_name: displayName.trim() || undefined,
        role,
        passcode,
      });
      onClose();
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="login-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title" id="login-title">
          Engineer / Admin login
        </div>
        <div className="modal-sub">
          Browsing works without an account — log in only to operate the DUT.
        </div>
        <form onSubmit={submit} className="modal-form">
          <label className="modal-label">
            Username
            <input
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              maxLength={64}
              autoFocus
              required
            />
          </label>
          <label className="modal-label">
            Display name
            <input
              className="input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              maxLength={64}
              placeholder="Shown to teammates (defaults to username)"
            />
          </label>
          <label className="modal-label">
            Role
            <select
              className="input"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              <option value="engineer">Engineer</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <label className="modal-label">
            {role === "admin" ? "Admin passcode" : "Engineer passcode"}
            <input
              className="input"
              type="password"
              value={passcode}
              onChange={(e) => setPasscode(e.target.value)}
              placeholder="Printed by start_lan.sh on the server"
              required
            />
          </label>
          {error ? <div className="flash">{error}</div> : null}
          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={busy || !username.trim()}>
              {busy ? "Logging in…" : "Log in"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
