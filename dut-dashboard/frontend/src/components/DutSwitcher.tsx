import { useEffect, useState } from "react";

import { addDut, DutInfo, getDuts, removeDut } from "../api/rest";
import { DEFAULT_DUT_ID } from "../api/dut";

/**
 * Topbar DUT selector (multi-DUT Stage 2a). Lists the registered DUTs, switches
 * which one the monitoring sections reflect, and manages add/remove. The
 * Serial Console stays pinned to the default DUT until Stage 2b.
 */
export default function DutSwitcher({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (dutId: string) => void;
}) {
  const [duts, setDuts] = useState<DutInfo[]>([]);
  const [manageOpen, setManageOpen] = useState(false);
  const [newId, setNewId] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [error, setError] = useState("");

  const refresh = () => {
    getDuts()
      .then(setDuts)
      .catch(() => setError("Could not load DUTs"));
  };

  useEffect(refresh, []);

  async function handleAdd() {
    setError("");
    try {
      await addDut(newId.trim(), newLabel.trim() || undefined);
      setNewId("");
      setNewLabel("");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Add failed");
    }
  }

  async function handleRemove(id: string) {
    if (!window.confirm(`Remove DUT "${id}"? Its serial connection will be closed.`)) {
      return;
    }
    setError("");
    try {
      await removeDut(id);
      if (id === selected) {
        onSelect(DEFAULT_DUT_ID);
      }
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    }
  }

  return (
    <div className="dut-switcher" style={{ position: "relative", display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
      <select
        aria-label="Selected DUT"
        value={selected}
        onChange={(e) => onSelect(e.target.value)}
        style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--panel)" }}
      >
        {duts.map((d) => (
          <option key={d.id} value={d.id}>
            {d.label}{d.serial_open ? " ●" : ""}
          </option>
        ))}
      </select>
      <button className="btn" onClick={() => setManageOpen((v) => !v)} title="Manage DUTs">
        DUTs
      </button>

      {manageOpen ? (
        <div
          className="card"
          style={{ position: "absolute", top: "calc(100% + 6px)", right: 0, zIndex: 20, width: 320, padding: "var(--space-3)" }}
        >
          <div style={{ fontWeight: 600, marginBottom: "var(--space-2)" }}>DUTs</div>
          {error ? <div className="flash" style={{ marginBottom: "var(--space-2)" }}>{error}</div> : null}
          <div style={{ display: "grid", gap: "var(--space-1)", marginBottom: "var(--space-3)" }}>
            {duts.map((d) => (
              <div key={d.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span>{d.label} <span style={{ color: "var(--faint)" }}>({d.id})</span></span>
                {d.removable ? (
                  <button
                    className="btn"
                    onClick={() => handleRemove(d.id)}
                    style={{ padding: "2px 10px", color: "var(--danger)", borderColor: "var(--danger)" }}
                  >
                    Remove
                  </button>
                ) : (
                  <span style={{ color: "var(--faint)", fontSize: 12 }}>fixed</span>
                )}
              </div>
            ))}
          </div>
          <div style={{ display: "grid", gap: "var(--space-2)" }}>
            <input
              placeholder="id (a-z 0-9 - _)"
              value={newId}
              onChange={(e) => setNewId(e.target.value)}
              style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--border)" }}
            />
            <input
              placeholder="label (optional)"
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
              style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--border)" }}
            />
            <button className="btn" onClick={handleAdd} disabled={!newId.trim()}>
              Add DUT
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
