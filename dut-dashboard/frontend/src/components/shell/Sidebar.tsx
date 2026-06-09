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
        <div className="nav-section">Monitoring</div>
        {NAV_ITEMS.map((item) => (
          <button
            key={item.id}
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
        ))}
      </nav>
    </aside>
  );
}
