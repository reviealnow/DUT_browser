import { getConsoleTail, getSnapshots } from "../api/rest";
import {
  connectDashboardWebSocket,
  connectFleetWebSocket,
  DashboardSocket,
  DashboardSocketHandlers,
  FleetSocketHandlers,
  SnapshotPayload,
} from "../api/websocket";
import type { IDataSource } from "./IDataSource";

/**
 * Live mode: the shared, self-reconnecting `/ws` plus the two REST backfill
 * routes. A thin adapter on purpose — the socket's reconnect, supersede and
 * delta logic stay in `api/websocket.ts`, where they were already tested, and
 * are not re-implemented here.
 */
export class WebSocketDataSource implements IDataSource {
  subscribe(handlers: DashboardSocketHandlers, dutId: string): DashboardSocket {
    return connectDashboardWebSocket(handlers, dutId);
  }

  subscribeFleet(handlers: FleetSocketHandlers): DashboardSocket {
    return connectFleetWebSocket(handlers);
  }

  loadSnapshots(limit: number, dutId: string): Promise<SnapshotPayload[]> {
    return getSnapshots(limit, dutId);
  }

  loadConsoleTail(limit: number, dutId: string): Promise<string[]> {
    return getConsoleTail(limit, dutId);
  }
}
