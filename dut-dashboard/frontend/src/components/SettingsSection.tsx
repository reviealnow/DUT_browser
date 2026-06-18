import { ACCENT_PRESETS, useSettings } from "../monitoring/useSettings";
import { Card } from "./shell/Card";

const BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];

export default function SettingsSection() {
  const { settings, setAccent, setDefaultBaud, setDisplayName } = useSettings();

  return (
    <Card title="Settings" subtitle="Saved in this browser">
      <div className="settings-list">
        <div className="setting-row">
          <div>
            <div className="setting-label">Display name</div>
            <div className="setting-hint">
              Tagged as the uploader / author in the Files and Bulletin workspace. Optional — left blank shows “—”.
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

        <div className="setting-row">
          <div>
            <div className="setting-label">Critical-crash keywords</div>
            <div className="setting-hint">
              Keywords you lock in the Critical Crash panel are remembered in this browser.
            </div>
          </div>
          <span className="setting-hint">Managed in the Serial Console crash panel</span>
        </div>
      </div>
    </Card>
  );
}
