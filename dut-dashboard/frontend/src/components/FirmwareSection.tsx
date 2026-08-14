/**
 * Admin firmware upgrade (P72b).
 *
 * Uploads a customer-signed .sig to the DUT's management API. The image can be
 * picked up here directly or chosen from the shared Files workspace, which is
 * where it is stored either way. The guardrails are the point of this component:
 * the checksum
 * is shown and can be matched against the customer's published value, and the
 * confirm dialog spells out what a power loss does and stays disabled until the
 * DUT's name is typed back.
 *
 * The form is four labelled rows ending in one filled button, because an
 * operator holding a firmware image is making four decisions and then
 * committing. Two things are deliberately not here:
 *
 * - A "rehearse (dry run)" button beside the real one. Everything a rehearsal
 *   proved -- the checksum, the image/transport pairing, the target URL -- is
 *   on screen before you click, so the button bought nothing and cost the
 *   destructive action its visual primacy. The dry-run *capability* is
 *   untouched: `DUT_FIRMWARE_DRY_RUN` still makes a deployment unable to flash,
 *   and this form turns into a rehearse-only form when it is set.
 * - A <select> for the upload path. Two options that take different, non-
 *   interchangeable images are a radio group; a dropdown hides the branch you
 *   did not pick, which is the one you need in order to check you picked right.
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
  uploadFile,
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
  const [source, setSource] = useState<"upload" | "workspace">("upload");
  const [expected, setExpected] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [transport, setTransport] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
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

  /** Upload an image from here rather than sending the operator to Files first.
   *
   * It still lands in the same shared workspace -- this is the same endpoint the
   * Files section uses, so the image stays listed, checksummed and attributable.
   * Routing a firmware upgrade through another section just to get the file in
   * was the wrong shape: the one place you need the image is this one. */
  const uploadImage = async (file: File) => {
    setUploading(true);
    setError(null);
    setResult(null);
    try {
      const row = await uploadFile(file);
      const listing = await getFiles();
      setFiles(listing.files);
      // Select what was just uploaded: the operator's intent is unambiguous,
      // and the stored name can differ from the local one when the workspace
      // de-duplicates it.
      setSelected(row.id);
    } catch (err) {
      setError(humanizeApiError(err));
    } finally {
      setUploading(false);
    }
  };

  const chosen = files.find((f) => f.id === selected) ?? null;
  const dut = config?.duts.find((d) => d.id === dutId) ?? null;
  const dryRunForced = config?.dry_run ?? false;

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

  // Split the workspace listing rather than filtering it. Every file in there is
  // still reachable — the point is that a listing of session logs and CSVs is
  // not a firmware picker, so the images come first under their own heading.
  const images = files.filter((f) => imageKind(f.filename) !== "unknown");
  const others = files.filter((f) => imageKind(f.filename) === "unknown");

  // A forced dry run cannot reach the DUT, so it needs neither credentials nor
  // the type-the-name confirmation — which is exactly what the removed rehearse
  // button used to offer, kept here instead of dropped.
  const needsDut = !dryRunForced;
  const blocker = !chosen
    ? "Choose a firmware image to continue."
    : mismatch
      ? mismatch
      : needsDut && !dut?.mgmt_url
        ? "No management address for this DUT — set it under DUT access below."
        : needsDut && !(config?.has_credentials ?? false)
          ? "No DUT API credentials stored — set them under DUT access below."
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

          <div className="fw-field">
            <div className="fw-field-label" id="fw-path-label">
              Upload path
            </div>
            <div className="fw-field-control">
              <div className="fw-choices" role="radiogroup" aria-labelledby="fw-path-label">
                {transports.map((t) => (
                  <label key={t.id} className={`fw-choice ${transport === t.id ? "active" : ""}`}>
                    <input
                      type="radio"
                      name="fw-transport"
                      value={t.id}
                      checked={transport === t.id}
                      onChange={() => setTransport(t.id)}
                      disabled={busy}
                    />
                    <span className="fw-choice-main">{t.label}</span>
                    <span className="fw-choice-note">
                      Takes the <strong>{t.image}</strong> image only · POSTs to{" "}
                      <code>{t.path}</code> on port {t.port}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          </div>

          <div className="fw-field">
            <div className="fw-field-label" id="fw-source-label">
              Firmware image
            </div>
            <div className="fw-field-control">
              <div
                className="fw-choices inline"
                role="radiogroup"
                aria-labelledby="fw-source-label"
              >
                <label className={`fw-choice ${source === "upload" ? "active" : ""}`}>
                  <input
                    type="radio"
                    name="fw-source"
                    checked={source === "upload"}
                    onChange={() => setSource("upload")}
                    disabled={busy}
                  />
                  <span className="fw-choice-main">a file on your PC</span>
                </label>
                <label className={`fw-choice ${source === "workspace" ? "active" : ""}`}>
                  <input
                    type="radio"
                    name="fw-source"
                    checked={source === "workspace"}
                    onChange={() => setSource("workspace")}
                    disabled={busy}
                  />
                  <span className="fw-choice-main">
                    already in the Files workspace ({images.length})
                  </span>
                </label>
              </div>

              {source === "upload" ? (
                <>
                  <input
                    className="input"
                    type="file"
                    accept=".sig,.bin"
                    aria-label="Firmware image to upload"
                    disabled={uploading || busy}
                    onChange={(e) => {
                      const picked = e.target.files?.[0];
                      // Clear the input so picking the same file twice still fires a
                      // change event, e.g. after a failed upload.
                      e.target.value = "";
                      if (picked) {
                        void uploadImage(picked);
                      }
                    }}
                  />
                  <div className="setting-hint">
                    {uploading
                      ? "Uploading…"
                      : "Lands in the shared Files workspace — checksummed and attributable — and is selected here."}
                  </div>
                </>
              ) : (
                <select
                  className="input"
                  aria-label="Firmware image from the workspace"
                  value={selected ?? ""}
                  disabled={busy}
                  onChange={(e) => setSelected(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">Select an uploaded file…</option>
                  {images.length ? (
                    <optgroup label="Firmware images">
                      {images.map((file) => (
                        <option key={file.id} value={file.id}>
                          {file.filename} · {imageKind(file.filename)} ·{" "}
                          {Math.round(file.size / 1024 / 1024)} MB
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                  {others.length ? (
                    <optgroup label="Other workspace files (not recognised as firmware)">
                      {others.map((file) => (
                        <option key={file.id} value={file.id}>
                          {file.filename} · {Math.round(file.size / 1024 / 1024)} MB
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                </select>
              )}

              {chosen ? (
                <div className="fw-chosen">
                  <strong>{chosen.filename}</strong>
                  <span>
                    {Math.round(chosen.size / 1024 / 1024)} MB · {chosenKind} image
                  </span>
                </div>
              ) : null}

              {mismatch ? (
                <div className="flash" style={{ color: "var(--danger)" }}>
                  {mismatch}
                </div>
              ) : null}
            </div>
          </div>

          <div className="fw-field">
            <div className="fw-field-label">Checksum</div>
            <div className="fw-field-control">
              <div className="setting-hint">
                SHA-256 of the selected image:{" "}
                <code className="invite-url">
                  {chosen
                    ? (chosen.sha256 ?? "not recorded (uploaded before checksums)")
                    : "— no image selected"}
                </code>
              </div>
              <input
                className="input"
                aria-label="Expected SHA-256"
                value={expected}
                onChange={(e) => setExpected(e.target.value)}
                placeholder="optional — paste the customer's published checksum"
                spellCheck={false}
                disabled={busy}
              />
              <div className="setting-hint">
                Optional. If filled in, a value that does not match blocks the upload.
              </div>
            </div>
          </div>

          <div className="fw-action">
            <div className="fw-action-summary">
              {blocker ? (
                blocker
              ) : dryRunForced ? (
                <>
                  Rehearsal only — this deployment has <code>DUT_FIRMWARE_DRY_RUN</code> set and
                  nothing will be sent to <strong>{dutId}</strong>.
                </>
              ) : (
                <>
                  Will flash <strong>{chosen?.filename}</strong> to <strong>{dutId}</strong> at{" "}
                  <code>
                    {dut?.mgmt_url}
                    {activeTransport?.path}
                  </code>{" "}
                  via {activeTransport?.label}.
                </>
              )}
            </div>
            <button
              type="button"
              className="btn danger-btn fw-flash"
              disabled={Boolean(blocker) || busy}
              onClick={() => (dryRunForced ? void flash(true) : setConfirming(true))}
            >
              {busy
                ? "Working…"
                : dryRunForced
                  ? "Rehearse upgrade (dry run)"
                  : "Upgrade firmware…"}
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
