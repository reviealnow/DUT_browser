/**
 * Shown when the page is opened from an invite QR/link (`/?invite=<token>`).
 *
 * One field, one button: the whole point of an invite is that the holder types
 * nothing but a name. The granted role is deliberately not previewed — the
 * backend has no endpoint to inspect an invite without spending a use — so the
 * dialog reports it only after joining.
 */
import { FormEvent, useEffect, useState } from "react";

import { getWhoami, humanizeApiError } from "../api/rest";
import { useAuth } from "../monitoring/AuthContext";

type Props = {
  token: string;
  onClose: () => void;
};

export default function InviteRedeemDialog({ token, onClose }: Props) {
  const { redeem } = useAuth();
  const [username, setUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getWhoami()
      .then((who) => {
        if (!cancelled) {
          setUsername((current) => current || who.name);
        }
      })
      .catch(() => {
        // Suggestion only — the user can type their own.
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
      await redeem(token, username.trim());
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
        aria-labelledby="invite-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title" id="invite-title">
          You have an invite
        </div>
        <div className="modal-sub">
          Join with a name so teammates can tell who is on the dashboard.
        </div>
        <form onSubmit={submit} className="modal-form">
          <label className="modal-label">
            Your name
            <input
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              maxLength={64}
              autoFocus
              required
            />
          </label>
          {error ? <div className="flash">{error}</div> : null}
          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              Not now
            </button>
            <button type="submit" className="btn primary" disabled={busy || !username.trim()}>
              {busy ? "Joining…" : "Join"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
