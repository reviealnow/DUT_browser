// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MemorySeries, Role } from "../api/rest";
import type { DutMonitorState, MemorySample } from "../monitoring/useDutMonitor";

/**
 * The Memory trend card between two sysMon blocks.
 *
 * The parser opens every `= Test Time` block with `memory: {}` and emits once
 * per CPU line before the meminfo lines arrive, so for a moment in every block
 * the live sample is null. The card used to take that as "no live memory" and
 * swap to the post-analysis body, which fetches /api/analyzer/memory on mount:
 * one request per Test Time, a 401 (and the /api/auth/me re-check it triggers)
 * for every guest, and for an engineer a flash of a different session's
 * analyzer curve. These tests pin both halves of the fix: the live trend holds
 * across the boundary, and a guest never asks the engineer-gated route at all.
 */

let role: Role = "guest";
let loading = false;
vi.mock("../monitoring/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/AuthContext")>()),
  useAuth: () => ({ role, loading }),
}));

const getMemory = vi.fn<(limit?: number) => Promise<MemorySeries>>();
vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getMemory: (limit?: number) => getMemory(limit),
}));

import { MemoryTrendBody } from "./AppShell";

const sample = (ts: string, effectiveMb: number): MemorySample => ({
  ts,
  memTotalKb: 1_036_288,
  memFreeKb: 400_000,
  memAvailableKb: (effectiveMb + 40) * 1024,
  buffersKb: 9_000,
  cachedKb: 150_000,
  slabKb: 60_000,
  sunreclaimKb: 40 * 1024,
  effectiveKb: effectiveMb * 1024,
});

const monitorWith = (memoryLive: MemorySample | null, memoryHistory: MemorySample[]) =>
  ({ memoryLive, memoryHistory }) as unknown as DutMonitorState;

const HISTORY = [sample("2026-10-06 09:00:00", 470), sample("2026-10-06 09:01:17", 460)];

describe("MemoryTrendBody", () => {
  beforeEach(() => {
    role = "guest";
    loading = false;
    getMemory.mockReset();
    getMemory.mockResolvedValue({ available: false, points: [] });
  });
  afterEach(cleanup);

  it("keeps the live trend while a new Test Time has no memory yet", () => {
    const { rerender } = render(<MemoryTrendBody monitor={monitorWith(HISTORY[1], HISTORY)} />);
    expect(screen.getByText(/effective avail · live/)).toBeTruthy();

    // The block boundary: CPU lines of the next Test Time, meminfo not yet read.
    rerender(<MemoryTrendBody monitor={monitorWith(null, HISTORY)} />);

    expect(screen.getByText(/effective avail · live/)).toBeTruthy();
    expect(screen.getByText("460")).toBeTruthy();
    expect(getMemory).not.toHaveBeenCalled();
  });

  it("shows the last complete reading while meminfo keys are still streaming in", () => {
    const partial = { ...sample("2026-10-06 09:02:34", 0), memAvailableKb: null, effectiveKb: null };
    render(<MemoryTrendBody monitor={monitorWith(partial, HISTORY)} />);
    expect(screen.getByText("460")).toBeTruthy();
  });

  it("does not ask a guest's browser for the engineer-gated analyzer feed", () => {
    render(<MemoryTrendBody monitor={monitorWith(null, [])} />);
    expect(screen.getByText(/needs an engineer login/)).toBeTruthy();
    expect(getMemory).not.toHaveBeenCalled();
  });

  it("loads the analyzer feed for an engineer when there is no live memory", async () => {
    role = "engineer";
    render(<MemoryTrendBody monitor={monitorWith(null, [])} />);
    expect(await screen.findByText("No memory data yet")).toBeTruthy();
    expect(getMemory).toHaveBeenCalledTimes(1);
  });

  it("waits for the session check, then loads once the role turns out to be engineer", async () => {
    loading = true;
    const { rerender } = render(<MemoryTrendBody monitor={monitorWith(null, [])} />);
    expect(screen.queryByText(/needs an engineer login/)).toBeNull();
    expect(getMemory).not.toHaveBeenCalled();

    loading = false;
    role = "engineer";
    rerender(<MemoryTrendBody monitor={monitorWith(null, [])} />);
    expect(await screen.findByText("No memory data yet")).toBeTruthy();
    expect(getMemory).toHaveBeenCalledTimes(1);
  });
});
