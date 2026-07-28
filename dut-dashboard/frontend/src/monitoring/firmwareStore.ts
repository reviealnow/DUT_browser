/**
 * Module-level store for firmware_progress /ws events (P72b).
 *
 * Same shape as siteSurveyStore and for the same reason: the events arrive on
 * the shared useDutMonitor socket, but the panel that renders them mounts and
 * unmounts independently, so progress lives outside React state and components
 * subscribe.
 */

export const FIRMWARE_STAGES = [
  "preparing",
  "publishing",
  "instructing",
  "flashing",
  "done",
] as const;

export type FirmwareStage = (typeof FIRMWARE_STAGES)[number];

export type FirmwareProgress = {
  stage: FirmwareStage;
  detail: string;
  dryRun: boolean;
};

const _progress: Record<string, FirmwareProgress | null> = {};
const _subscribers = new Set<() => void>();

export function setFirmwareProgress(dutId: string, progress: FirmwareProgress | null): void {
  _progress[dutId] = progress;
  _subscribers.forEach((cb) => cb());
}

export function firmwareProgressFor(dutId: string): FirmwareProgress | null {
  return _progress[dutId] ?? null;
}

export function subscribeFirmware(cb: () => void): () => void {
  _subscribers.add(cb);
  return () => {
    _subscribers.delete(cb);
  };
}

/** 0..1 for a determinate bar; `done` is the last stage, so it lands on 1. */
export function stageFraction(stage: FirmwareStage): number {
  const index = FIRMWARE_STAGES.indexOf(stage);
  return (index + 1) / FIRMWARE_STAGES.length;
}
