import { useRef, useState } from "react";

import { useCrashKeywords } from "../monitoring/useCrashKeywords";
import { ACCENT_PRESETS, useSettings } from "../monitoring/useSettings";
import { Card } from "./shell/Card";

const BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];

export default function SettingsSection() {
  const { settings, setAccent, setDefaultBaud, setDisplayName } = useSettings();
  const { keywords, saving, saveKeywords } = useCrashKeywords();
  const [kwInput, setKwInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function addKeyword() {
    const kw = kwInput.trim();
    if (!kw) return;
    if (keywords.some((k) => k.toLowerCase() === kw.toLowerCase())) {
      setKwInput("");
      return;
    }
    void saveKeywords([...keywords, kw]);
    setKwInput("");
    inputRef.current?.focus();
  }

  function removeKeyword(kw: string) {
    void saveKeywords(keywords.filter((k) => k.toLowerCase() !== kw.toLowerCase()));
  }

  return (
    <>
      <Card title="Settings" subtitle="Saved in this browser">
        <div className="settings-list">
          <div className="setting-row">
            <div>
              <div className="setting-label">Display name</div>
              <div className="setting-hint">
                Tagged as the uploader / author in the Files and Bulletin workspace. Optional — left blank shows "—".
              </div>
            </div>
            <input
              type="text"
              value={settings.displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. nelson"
              aria-label="Display name"
              maxLength={40}
            />
          </div>

          <div className="setting-row">
            <div>
              <div className="setting-label">Accent colour</div>
              <div className="setting-hint">Theme accent for the whole dashboard.</div>
            </div>
            <div className="accent-swatches">
              {ACCENT_PRESETS.map((preset) => (
                <button
                  key={preset.accent}
                  type="button"
                  className={`accent-swatch${preset.accent === settings.accent ? " active" : ""}`}
                  style={{ background: preset.accent }}
                  title={preset.name}
                  aria-label={preset.name}
                  aria-pressed={preset.accent === settings.accent}
                  onClick={() => setAccent(preset.accent)}
                />
              ))}
            </div>
          </div>

          <div className="setting-row">
            <div>
              <div className="setting-label">Default baud rate</div>
              <div className="setting-hint">Pre-fills the serial connection form.</div>
            </div>
            <select
              value={settings.defaultBaud}
              onChange={(e) => setDefaultBaud(Number(e.target.value))}
              aria-label="Default baud rate"
            >
              {BAUD_RATES.map((rate) => (
                <option key={rate} value={rate}>
                  {rate}
                </option>
              ))}
            </select>
          </div>
        </div>
      </Card>

      <Card title="Crash Keywords" subtitle="Persisted on the server · used by all monitors">
        <div className="settings-list">
          <div className="setting-hint" style={{ marginBottom: 8 }}>
            Any log line matching one of these keywords increments the Critical Crash counter and appears
            in the crash panel. An empty list disables crash detection.
          </div>
          <div className="kw-chips">
            {keywords.length === 0 && (
              <span className="setting-hint" style={{ fontStyle: "italic" }}>
                No keywords — crash detection disabled.
              </span>
            )}
            {keywords.map((kw) => (
              <span key={kw} className="kw-chip">
                {kw}
                <button
                  type="button"
                  className="kw-chip-remove"
                  aria-label={`Remove "${kw}"`}
                  disabled={saving}
                  onClick={() => removeKeyword(kw)}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
          <div className="kw-add-row">
            <input
              ref={inputRef}
              type="text"
              value={kwInput}
              onChange={(e) => setKwInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addKeyword();
                }
              }}
              placeholder="Add keyword…"
              aria-label="New crash keyword"
              maxLength={120}
              disabled={saving}
            />
            <button type="button" onClick={addKeyword} disabled={saving || !kwInput.trim()}>
              Add
            </button>
          </div>
        </div>
      </Card>
    </>
  );
}
