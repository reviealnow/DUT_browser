import { ReactNode } from "react";

type Props = {
  title: string;
  subtitle: string;
  /** Optional search slot — only render where the current data supports filtering. */
  search?: ReactNode;
  /** Right-aligned status pills + primary action. */
  actions?: ReactNode;
  /** Opens the mobile nav drawer. The button only renders under the 720px
   * breakpoint (hidden on desktop, where the sidebar is always visible). */
  onMenuClick?: () => void;
  navOpen?: boolean;
};

export default function Topbar({ title, subtitle, search, actions, onMenuClick, navOpen }: Props) {
  return (
    <header className="toolbar">
      <button
        type="button"
        className="hamburger"
        onClick={onMenuClick}
        aria-label="Open navigation menu"
        aria-controls="app-sidebar"
        aria-expanded={navOpen ?? false}
      >
        ☰
      </button>
      <div className="toolbar-titles">
        <div className="toolbar-title">{title}</div>
        {/* Omitted, not blanked: an empty .toolbar-sub still occupies its line
            height and pushes the title off the header's centre. A section whose
            title says enough on its own passes "". */}
        {subtitle ? <div className="toolbar-sub">{subtitle}</div> : null}
      </div>
      <div className="toolbar-spacer" />
      {search}
      {actions}
    </header>
  );
}
