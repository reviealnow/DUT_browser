/**
 * Admin firmware upgrade (P72b).
 *
 * Uploads a customer-signed .sig from the Files workspace to the DUT's
 * management API. The guardrails are the point of this component: the checksum
 * is shown and can be matched against the customer's published value, and the
 * confirm dialog spells out what a power loss does and stays disabled until the
 * DUT's name is typed back.
 */
import { FormEvent, useEffect, useState, useSyncExternalStore } from "react";

import {
  FirmwareConfig,
  FirmwareDut,
  getFiles,
  getFirmwareConfig,
  humanizeApiError,
  setDutMgmtUrl,
  setFirmwareCredentials,
  imageKind,
  upgradeFirmware,
  WorkspaceFile,
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
  const [expected, setExpected] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [transport, setTransport] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const progress = useSyncExternalStore(
    subscribeFirmware,
    () => firmwareProgressFor(dutId),
    () => null,
  );

  const reload = () => {
    getFirmwareConfig()
      .then((cfg) => {
        setConfig(cfg);
        // Adopt the backend's default once, so the two-image rule is decided in
        // one place rather than duplicated as a literal here.
        setTransport((current) => current || cfg.default_transport);
      })
      .catch((err) => setError(humanizeApiError(err)));
  };

  useEffect(() => {
    getFiles()
      .then((r) => setFiles(r.files))
      .catch((err) => setError(humanizeApiError(err)));
    reload();
  }, []);

  const chosen = files.find((f) => f.id === selected) ?? null;
  const dut = config?.duts.find((d) => d.id === dutId) ?? null;
  const dryRunForced = config?.dry_run ?? false;
  const ready = Boolean(dut?.mgmt_url) && (config?.has_credentials ?? false);

  const transports = config?.transports ?? [];
  const activeTransport = transports.find((t) => t.id === transport) ?? transports[0] ?? null;
  // The DUT accepts only one image type per upload path, so a mismatch is worth
  // saying out loud before the operator commits — the backend refuses it too,
  // but finding out at the confirm dialog is a worse way to learn.
  const chosenKind = chosen ? imageKind(chosen.filename) : "unknown";
  const mismatch =
    activeTransport && chosenKind !== "unknown" && chosenKind !== activeTransport.image
      ? `${chosen?.filename} is the ${chosenKind} image; “${activeTransport.label}” needs the ${activeTransport.image} one.`
      : null;

  const flash = async (rehearse: boolean) => {
    if (!chosen) {
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const outcome = await upgradeFirmware(chosen.id, dutId, {
        dryRun: rehearse,
        expectedSha256: expected.trim() || undefined,
        transport: transport || undefined,
      });
      setResult(
        outcome.dry_run
          ? `Dry run complete — nothing was sent. Would upload ${outcome.size} bytes to ${outcome.url} via the ${outcome.transport} transport (sha256 ${outcome.sha256}).`
          : (outcome.detail ?? "Upgrade accepted."),
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
      <Card
        title="Upgrade Firmware"
        subtitle={`Admin only · ${activeTransport ? activeTransport.label : "select an upload path"}`}
      >
        <div className="settings-list">
          {dryRunForced ? (
            <div className="pill warn" style={{ alignSelf: "flex-start" }}>
              DRY RUN — this deployment cannot flash
            </div>
          ) : null}
          {!ready ? (
            <div className="flash">
              {!dut?.mgmt_url
                ? "No management address set for this DUT — set it below before upgrading."
                : "No DUT API credentials stored — set them below before upgrading."}
            </div>
          ) : null}

          <label className="modal-label">
            Upload path
            <select
              className="input"
              value={transport}
              onChange={(e) => setTransport(e.target.value)}
            >
              {transports.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          {activeTransport ? (
            <div className="setting-hint">
              POSTs to <code>{activeTransport.path}</code> on port {activeTransport.port}. The DUT
              accepts only the <strong>{activeTransport.image}</strong> image here — the other path
              takes the other type, and they are not interchangeable.
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
                  {file.filename} ({Math.round(file.size / 1024 / 1024)} MB)
                </option>
              ))}
            </select>
          </label>

          {mismatch ? (
            <div className="flash" style={{ color: "var(--danger)" }}>
              {mismatch}
            </div>
          ) : null}

          {chosen ? (
            <div className="setting-hint">
              SHA-256:{" "}
              <code className="invite-url">{chosen.sha256 ?? "not recorded (uploaded before checksums)"}</code>
            </div>
          ) : null}

          <label className="modal-label">
            Expected SHA-256 (optional — from the customer; mismatch blocks the upload)
            <input
              className="input"
              value={expected}
              onChange={(e) => setExpected(e.target.value)}
              placeholder="paste the published checksum to verify against"
              spellCheck={false}
            />
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
              disabled={!chosen || busy || !ready}
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

      <Card title="DUT access" subtitle="Where to reach the management API, and as whom">
        <div className="settings-list">
          <MgmtUrlEditor dutId={dutId} dut={dut} onSaved={reload} />
          <CredentialsEditor config={config} onSaved={reload} />
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
            <div className="modal-title">Flash firmware to this DUT?</div>
            <div className="modal-sub">
              <strong>Losing power during a flash bricks the DUT.</strong> Do not unplug it or
              stop the dashboard until the DUT reports it has finished.
            </div>
            <div className="modal-form">
              <div className="setting-hint">
                <strong>{chosen.filename}</strong> ({Math.round(chosen.size / 1024 / 1024)} MB) →{" "}
                <strong>{dut?.mgmt_url}</strong>
                {config?.upgrade_path}
              </div>
              <div className="setting-hint">
                sha256 <code>{chosen.sha256 ?? "—"}</code>
                {expected.trim() ? " · will be checked against the value you entered" : ""}
              </div>
              <label className="modal-label">
                Type the DUT name <strong>{dutId}</strong> to confirm
                <input
                  className="input"
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                  autoFocus
                />
              </label>
              <div className="modal-actions">
                <button type="button" className="btn" onClick={() => setConfirming(false)} disabled={busy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn danger-btn"
                  disabled={busy || typed !== dutId}
                  onClick={() => void flash(false)}
                >
                  {busy ? "Flashing…" : "Flash now"}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

function MgmtUrlEditor({
  dutId,
  dut,
  onSaved,
}: {
  dutId: string;
  dut: FirmwareDut | null;
  onSaved: () => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(dut?.mgmt_url ?? "");
  }, [dut]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await setDutMgmtUrl(dutId, value);
      onSaved();
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={save}>
      <div className="setting-hint">
        Management address for <strong>{dutId}</strong>. A bare IP becomes https://; empty clears
        it and blocks upgrades.
      </div>
      <div className="kw-add-row">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="e.g. 192.168.30.50"
          aria-label="DUT management address"
          disabled={saving}
        />
        <button type="submit" disabled={saving}>
          Save
        </button>
      </div>
      {error ? <div className="flash">{error}</div> : null}
    </form>
  );
}

function CredentialsEditor({
  config,
  onSaved,
}: {
  config: FirmwareConfig | null;
  onSaved: () => void;
}) {
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setUser(config?.user ?? "");
  }, [config]);

  if (config === null) {
    return <EmptyState icon="⏳" message="Loading…" />;
  }

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await setFirmwareCredentials(user.trim(), password);
      // Never keep the password in component state after it is stored.
      setPassword("");
      onSaved();
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={save} style={{ marginTop: "var(--space-4)" }}>
      <div className="setting-hint">
        DUT API credentials. Stored on the server and never sent back to the browser —{" "}
        {config.has_credentials
          ? `currently set (user "${config.user}"); re-enter to change.`
          : "not set yet."}
      </div>
      <div className="kw-add-row">
        <input
          type="text"
          value={user}
          onChange={(e) => setUser(e.target.value)}
          placeholder="user"
          aria-label="DUT API user"
          autoComplete="off"
          disabled={saving}
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password"
          aria-label="DUT API password"
          autoComplete="new-password"
          disabled={saving}
        />
        <button type="submit" disabled={saving || !user.trim() || !password}>
          Save
        </button>
      </div>
      {error ? <div className="flash">{error}</div> : null}
    </form>
  );
}
