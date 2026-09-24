import { useEffect, useId, useRef, useState } from "react";

import { IconAccount, IconLogout } from "./icons";

/**
 * The signed-in identity in the toolbar: name, role, Logout.
 *
 * On desktop the panel is `display: contents` and reads inline exactly as the
 * chip always has; the toggle is hidden. Under 720px the toggle is an account
 * icon and the panel a small menu under it, so identity -- which does not
 * change while somebody works -- stops costing a row of the sticky toolbar.
 * The event age rides along in the menu there, because the row it used to sit
 * in is gone; on desktop it stays in the status cluster and this copy is
 * hidden.
 *
 * One Logout in the DOM, not a desktop and a phone copy, so there is only one
 * control for a test, a screen reader or a keyboard to find.
 */
export default function AccountMenu({
  displayName,
  username,
  role,
  eventAge,
  onLogout,
}: {
  displayName: string;
  username: string;
  role: string;
  eventAge: string | null;
  onLogout: () => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const panelId = useId();

  // Closed by a tap anywhere else and by Esc. Not by blur alone, the way the
  // sidebar's flyout does it: iOS Safari does not focus a tapped button, so a
  // menu waiting for the toggle to lose focus would never close there.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        toggleRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span ref={rootRef} className={`auth-chip${open ? " open" : ""}`}>
      <button
        ref={toggleRef}
        type="button"
        className="btn tb-icon-btn auth-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`Account: ${displayName}`}
        title={`${displayName} (${role})`}
        onClick={() => setOpen((v) => !v)}
      >
        <IconAccount />
      </button>
      <span id={panelId} className="auth-panel">
        <span className="auth-name" title={username}>
          {displayName}
        </span>
        <span className={`pill role-${role}`}>{role}</span>
        {eventAge ? <span className="toolbar-sub auth-age">{eventAge}</span> : null}
        <button
          type="button"
          className="btn"
          onClick={() => {
            setOpen(false);
            onLogout();
          }}
        >
          <IconLogout />
          Logout
        </button>
      </span>
    </span>
  );
}
