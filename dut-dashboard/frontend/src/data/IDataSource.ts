import type {
  DashboardSocket,
  DashboardSocketHandlers,
  FleetSocketHandlers,
  SnapshotPayload,
} from "../api/websocket";

/**
 * Where realtime DUT telemetry comes from.
 *
 * The monitors (`useDutMonitor`, `useFleetMonitor`) talk to this and nothing
 * else, so they do not know whether a reading came off a serial cable through
 * FastAPI or out of a JSON file on GitHub Pages. Two implementations:
 * `WebSocketDataSource` (live) and `ReplayDataSource` (demo); which one runs
 * is decided once, in `createDataSource.ts`.
 *
 * The shape follows the transport the app already had rather than a new one:
 * subscribing *is* connecting, and the returned handle's `close()` is
 * disconnecting. Live mode refcounts one shared `/ws` across every subscriber,
 * and a separate connect()/disconnect() pair would be a second owner of that
 * socket's lifetime. Events keep the existing `DashboardEvent` union, so every
 * consumer reads the same `SnapshotPayload` whichever source produced it.
 */
export interface IDataSource {
  /** One DUT's stream. Deltas arrive already applied, as `snapshot_update`. */
  subscribe(handlers: DashboardSocketHandlers, dutId: string): DashboardSocket;

  /** Every DUT's stream, raw and tagged with `dut_id` (the fleet demuxes). */
  subscribeFleet(handlers: FleetSocketHandlers): DashboardSocket;

  /** Recent full snapshots, oldest first, to seed charts on (re)connect. */
  loadSnapshots(limit: number, dutId: string): Promise<SnapshotPayload[]>;

  /** Recent console lines, oldest first, to seed the console on (re)connect. */
  loadConsoleTail(limit: number, dutId: string): Promise<string[]>;
}
