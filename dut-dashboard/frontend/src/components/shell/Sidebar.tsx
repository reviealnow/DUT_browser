import { Fragment } from "react";

import { NAV_ITEMS, SectionId } from "./navigation";

type Props = {
  active: SectionId;
  onSelect: (id: SectionId) => void;
};

export default function Sidebar({ active, onSelect }: Props) {
  return (
    <aside className="sidebar">
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
  );
}
