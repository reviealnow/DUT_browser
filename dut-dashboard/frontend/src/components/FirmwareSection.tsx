/**
 * Admin firmware upgrade (P72b).
 *
 * The destructive-action guardrails are the point of this component: the image
 * is chosen from the Files workspace, the confirm dialog states plainly what a
 * power loss does, and the flash button stays disabled until no upgrade command
 * is configured OR the operator has typed the DUT's name back.
 */
import { useEffect, useState, useSyncExternalStore } from "react";

import {
  WorkspaceFile,
  FirmwareConfig,
  getFiles,
  getFirmwareConfig,
  humanizeApiError,
  setFirmwareTemplate,
  upgradeFirmware,
} from "../api/rest";
import {
  firmwareProgressFor,
  stageFraction,
  subscribeFirmware,
} from "../monitoring/firmwareStore";
import { Card, EmptyState } from "./shell/Card";

export default function FirmwareSection({ dutId }: { dutId: string }) {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [config, setConfig] = useState<FirmwareConfig | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const progress = useSyncExternalStore(
    subscribeFirmware,
    () => firmwareProgressFor(dutId),
    () => null,
  );

  useEffect(() => {
    getFiles()
      .then((r) => setFiles(r.files))
      .catch((err) => setError(humanizeApiError(err)));
    getFirmwareConfig()
      .then(setConfig)
      .catch((err) => setError(humanizeApiError(err)));
  }, []);

  const chosen = files.find((f) => f.id === selected) ?? null;
  const dryRun = config?.dry_run ?? false;
  // A real flash needs a configured command; a dry run deliberately does not,
  // so the whole flow stays exercisable on hardware nobody wants to risk.
  const canFlash = dryRun || (config?.configured ?? false);

  const flash = async (rehearse: boolean) => {
    if (!chosen) {
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const outcome = await upgradeFirmware(chosen.id, dutId, rehearse);
      setResult(
        outcome.dry_run
          ? `Dry run complete — nothing was sent to the DUT. Command would have been: ${outcome.command}`
          : "Upgrade command completed.",
      );
      setConfirming(false);
      setTyped("");
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Card title="Upgrade Firmware" subtitle="Admin only · flashes the selected DUT">
        <div className="settings-list">
          {dryRun ? (
            <div className="pill warn" style={{ alignSelf: "flex-start" }}>
              DRY RUN — no command will reach the DUT
            </div>
          ) : null}
          {!canFlash ? (
            <div className="flash">
              No upgrade command configured, so a real flash will be refused. Set the DUT's
              upgrade command below (or run with DUT_FIRMWARE_DRY_RUN=1 to rehearse).
            </div>
          ) : null}

          <label className="modal-label">
            Firmware image (from the Files workspace)
            <select
              className="input"
              value={selected ?? ""}
              onChange={(e) => setSelected(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Select an uploaded file…</option>
              {files.map((file) => (
                <option key={file.id} value={file.id}>
                  {file.filename} ({Math.round(file.size / 1024)} KB)
                </option>
              ))}
            </select>
          </label>

          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            {/* Rehearsing needs no confirmation: it cannot reach the DUT. */}
            <button
              type="button"
              className="btn"
              disabled={!chosen || busy}
              onClick={() => void flash(true)}
            >
              Rehearse (dry run)
            </button>
            <button
              type="button"
              className="btn danger-btn"
              disabled={!chosen || busy}
              onClick={() => setConfirming(true)}
            >
              Upgrade firmware…
            </button>
          </div>

          {error ? <div className="flash">{error}</div> : null}
          {result ? <div className="setting-hint">{result}</div> : null}

          {progress ? (
            <div className="fw-progress">
              <div className="fw-progress-bar">
                <span style={{ width: `${Math.round(stageFraction(progress.stage) * 100)}%` }} />
              </div>
              <div className="setting-hint">
                {progress.stage}
                {progress.dryRun ? " (dry run)" : ""} — {progress.detail}
              </div>
            </div>
          ) : null}
        </div>
      </Card>

      <Card title="Upgrade command" subtitle="Shell command run on the DUT · {url} is the image URL">
        <div className="settings-list">
          <div className="setting-hint">
            Left empty, a real flash is refused rather than guessed. The dashboard publishes the
            image on its own port behind a single-use token and passes that URL as
            <code> {"{url}"}</code>; the command runs on the DUT over the serial console.
          </div>
          <TemplateEditor config={config} onSaved={setConfig} />
        </div>
      </Card>

      {confirming && chosen ? (
        <div className="modal-backdrop" onClick={() => setConfirming(false)}>
          <div
            className="modal card"
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-title">
              {dryRun ? "Rehearse upgrade?" : "Flash firmware to this DUT?"}
            </div>
            <div className="modal-sub">
              {dryRun ? (
                <>This is a dry run — the stages stream but nothing is sent to the DUT.</>
              ) : (
                <>
                  <strong>Losing power during a flash bricks the DUT.</strong> Do not unplug it,
                  close the serial session, or stop the dashboard until this finishes.
                </>
              )}
            </div>
            <div className="modal-form">
              <div className="setting-hint">
                Image: <strong>{chosen.filename}</strong> → DUT <strong>{dutId}</strong>
              </div>
              {!dryRun ? (
                <label className="modal-label">
                  Type the DUT name <strong>{dutId}</strong> to confirm
                  <input
                    className="input"
                    value={typed}
                    onChange={(e) => setTyped(e.target.value)}
                    autoFocus
                  />
                </label>
              ) : null}
              <div className="modal-actions">
                <button type="button" className="btn" onClick={() => setConfirming(false)} disabled={busy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn danger-btn"
                  disabled={busy || (!dryRun && typed !== dutId)}
                  onClick={() => void flash(false)}
                >
                  {busy ? "Working…" : dryRun ? "Run dry run" : "Flash now"}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

function TemplateEditor({
  config,
  onSaved,
}: {
  config: FirmwareConfig | null;
  onSaved: (config: FirmwareConfig) => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(config?.template ?? "");
  }, [config]);

  if (config === null) {
    return <EmptyState icon="⏳" message="Loading…" />;
  }

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const saved = await setFirmwareTemplate(value);
      onSaved({ ...config, ...saved });
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="kw-add-row">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="e.g. curl -k -o /tmp/fw.bin {url} && sysupgrade -n /tmp/fw.bin"
          aria-label="Upgrade command template"
          disabled={saving}
        />
        <button type="button" onClick={() => void save()} disabled={saving}>
          Save
        </button>
      </div>
      {error ? <div className="flash">{error}</div> : null}
    </>
  );
}
