import { createContext, ReactNode, useContext } from "react";

import { DutMonitorState } from "./useDutMonitor";

/**
 * Shares the single useDutMonitor() instance (one WebSocket) between the
 * Overview KPIs/charts and the embedded Serial Console (Dashboard), so there
 * is exactly one /ws connection and one source of stream truth.
 */
const DutMonitorContext = createContext<DutMonitorState | null>(null);

export function DutMonitorProvider({ value, children }: { value: DutMonitorState; children: ReactNode }) {
  return <DutMonitorContext.Provider value={value}>{children}</DutMonitorContext.Provider>;
}

export function useDutMonitorContext(): DutMonitorState {
  const ctx = useContext(DutMonitorContext);
  if (ctx === null) {
    throw new Error("useDutMonitorContext must be used within a DutMonitorProvider");
  }
  return ctx;
}
