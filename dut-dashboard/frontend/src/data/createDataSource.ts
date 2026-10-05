import { APP_MODE, AppMode } from "./appMode";
import type { IDataSource } from "./IDataSource";
import { ReplayDataSource } from "./ReplayDataSource";
import { WebSocketDataSource } from "./WebSocketDataSource";

/**
 * The one place that maps the operating mode to a telemetry source. Components
 * and hooks never branch on the mode to decide where data comes from — they
 * ask `getDataSource()`.
 */
export function createDataSource(mode: AppMode = APP_MODE): IDataSource {
  if (mode === "demo") {
    return new ReplayDataSource();
  }
  return new WebSocketDataSource();
}

let instance: IDataSource | null = null;

/**
 * The app-wide source. One instance, because both monitors must see the same
 * stream: live mode refcounts one `/ws`, and demo mode runs one replay clock
 * that the Overview strip and the selected-DUT monitor have to agree on.
 */
export function getDataSource(): IDataSource {
  if (instance === null) {
    instance = createDataSource();
  }
  return instance;
}

/** Test seam: install a fake source (or `null` to rebuild from the mode). */
export function setDataSourceForTests(source: IDataSource | null): void {
  instance = source;
}
