import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import { useAuth } from "../../monitoring/AuthContext";
import { groupedNavItems, NavGroup, NavItem, SectionId } from "./navigation";

type Props = {
  active: SectionId;
  onSelect: (id: SectionId) => void;
  /** Mobile off-canvas drawer state. On desktop the sidebar is always visible
   * and these are inert (the drawer CSS only applies under the 720px breakpoint). */
  open: boolean;
  onClose: () => void;
  /** Desktop rail state, owned by AppShell because the grid column is on .app. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
};

const MOBILE_QUERY = "(max-width: 720px)";

/**
 * True under the drawer breakpoint. The rail and the drawer are two different
 * answers to "no room for a 232px sidebar" and must not both apply: a 64px rail
 * sliding out over the page would be a menu you cannot read the labels of.
 * Below 720px the drawer wins and the rail state is ignored (not forgotten —
 * it comes back when the window widens again).
 */
function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", onChange);
    setIsMobile(mq.matches);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return isMobile;
}

export default function Sidebar({
  active,
  onSelect,
  open,
  onClose,
  collapsed,
  onToggleCollapsed,
}: Props) {
  // Role-filtered nav (cosmetic; the backend enforces). Groups are derived from
  // the filtered list, so a group whose every item is hidden leaves neither an
  // orphan header nor an empty rail button behind.
  const { role } = useAuth();
  const groups = groupedNavItems(role);
  const isMobile = useIsMobile();
  const rail = collapsed && !isMobile;
  // Which rail group has its flyout open. One at a time — the flyout is
  // absolutely positioned and two of them would overlap.
  const [openGroup, setOpenGroup] = useState<NavGroup | null>(null);

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

  // A flyout left open while the rail expands (or the window narrows into the
  // drawer) would hang over a sidebar that no longer has a button under it.
  useEffect(() => {
    if (!rail) {
      setOpenGroup(null);
    }
  }, [rail]);

  const select = useCallback(
    (id: SectionId) => {
      setOpenGroup(null);
      onSelect(id);
    },
    [onSelect],
  );

  return (
    <>
      <div className={`sidebar-backdrop${open ? " open" : ""}`} onClick={onClose} aria-hidden />
      <aside
        id="app-sidebar"
        className={`sidebar${open ? " open" : ""}${rail ? " rail" : ""}`}
      >
        <div className="brand">
          <div className="brand-mark">D</div>
          {!rail ? (
            <div>
              <div className="brand-name">DUT Console</div>
              <div className="brand-sub">Lab monitoring</div>
            </div>
          ) : null}
          <button
            type="button"
            className="nav-collapse"
            onClick={onToggleCollapsed}
            aria-controls="app-sidebar"
            aria-expanded={!rail}
            aria-label={rail ? "Expand navigation" : "Collapse navigation"}
            title={rail ? "Expand navigation" : "Collapse navigation"}
          >
            <span aria-hidden>{rail ? "»" : "«"}</span>
          </button>
        </div>
        <nav className="nav" aria-label="Sections">
          {groups.map((group) =>
            rail ? (
              <RailGroup
                key={group.group}
                group={group.group}
                icon={group.icon}
                items={group.items}
                active={active}
                open={openGroup === group.group}
                onOpenChange={(next) => setOpenGroup(next ? group.group : null)}
                onSelect={select}
              />
            ) : (
              <Fragment key={group.group}>
                <div className="nav-section">{group.group}</div>
                {group.items.map((item) => (
                  <NavButton key={item.id} item={item} active={active} onSelect={select} />
                ))}
              </Fragment>
            ),
          )}
        </nav>
      </aside>
    </>
  );
}

function NavButton({
  item,
  active,
  onSelect,
}: {
  item: NavItem;
  active: SectionId;
  onSelect: (id: SectionId) => void;
}) {
  return (
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
  );
}

/**
 * One group on the collapsed rail: an icon button that opens its sections in a
 * flyout beside the rail.
 *
 * Hover opens it because that is what a rail is for, but hover alone would
 * leave the sections unreachable by keyboard and by touch, so click toggles and
 * focus opens too. It closes on Esc (focus goes back to the button), when focus
 * leaves the group entirely, and when a section is chosen.
 */
function RailGroup({
  group,
  icon,
  items,
  active,
  open,
  onOpenChange,
  onSelect,
}: {
  group: NavGroup;
  icon: string;
  items: NavItem[];
  active: SectionId;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: (id: SectionId) => void;
}) {
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  // Esc closes the flyout and hands focus back to the button — which fires
  // onFocus, which would open it straight back up. The flag swallows exactly
  // that one focus event; it is only ever set when focus actually moves, so it
  // cannot go stale and eat a later tab-in.
  const suppressFocusOpen = useRef(false);
  const holdsActive = items.some((item) => item.id === active);
  const flyoutId = `nav-flyout-${group.toLowerCase()}`;

  return (
    <div
      className="rail-group"
      onMouseEnter={() => onOpenChange(true)}
      onMouseLeave={() => onOpenChange(false)}
      onBlur={(e) => {
        // Only when focus left the group for good — moving between the button
        // and an item inside the flyout must not close it under the keyboard.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          onOpenChange(false);
        }
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.stopPropagation();
          const button = buttonRef.current;
          if (button && document.activeElement !== button) {
            suppressFocusOpen.current = true;
            button.focus();
          }
          onOpenChange(false);
        }
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        className={`nav-item rail-item${holdsActive ? " active" : ""}`}
        onClick={() => onOpenChange(!open)}
        onFocus={() => {
          if (suppressFocusOpen.current) {
            suppressFocusOpen.current = false;
            return;
          }
          onOpenChange(true);
        }}
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={flyoutId}
        title={group}
      >
        <span className="nav-ico" aria-hidden>
          {icon}
        </span>
        <span className="sr-only">{group}</span>
      </button>
      <div id={flyoutId} className="rail-flyout" role="group" aria-label={group} hidden={!open}>
        <div className="nav-section">{group}</div>
        {items.map((item) => (
          <NavButton key={item.id} item={item} active={active} onSelect={onSelect} />
        ))}
      </div>
    </div>
  );
}
