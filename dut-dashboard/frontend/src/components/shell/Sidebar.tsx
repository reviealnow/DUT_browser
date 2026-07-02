import { Fragment, useEffect } from "react";

import { NAV_ITEMS, SectionId } from "./navigation";

type Props = {
  active: SectionId;
  onSelect: (id: SectionId) => void;
  /** Mobile off-canvas drawer state. On desktop the sidebar is always visible
   * and these are inert (the drawer CSS only applies under the 720px breakpoint). */
  open: boolean;
  onClose: () => void;
};

export default function Sidebar({ active, onSelect, open, onClose }: Props) {
  // While the mobile drawer is open, Esc closes it and the page behind it is
  // locked from scrolling. Both are no-ops on desktop (open stays false there).
  useEffect(() => {
    if (!open) {
      return;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  return (
    <>
      <div className={`sidebar-backdrop${open ? " open" : ""}`} onClick={onClose} aria-hidden />
      <aside id="app-sidebar" className={`sidebar${open ? " open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">D</div>
          <div>
            <div className="brand-name">DUT Console</div>
            <div className="brand-sub">Lab monitoring</div>
          </div>
        </div>
        <nav className="nav">
          {NAV_ITEMS.map((item, index) => (
            <Fragment key={item.id}>
              {/* A group header is rendered whenever the group changes. */}
              {index === 0 || NAV_ITEMS[index - 1].group !== item.group ? (
                <div className="nav-section">{item.group}</div>
              ) : null}
              <button
                type="button"
                className={`nav-item${item.id === active ? " active" : ""}`}
                onClick={() => onSelect(item.id)}
                aria-current={item.id === active ? "page" : undefined}
              >
                <span className="nav-ico" aria-hidden>
                  {item.icon}
                </span>
                <span>{item.label}</span>
              </button>
            </Fragment>
          ))}
        </nav>
      </aside>
    </>
  );
}
