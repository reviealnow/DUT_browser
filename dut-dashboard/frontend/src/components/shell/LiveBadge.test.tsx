// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveBadge, ScanAge } from "./LiveBadge";

/**
 * The card-header liveness slot. A stream-fed card says LIVE only while it is
 * live; a scan-fed card never says LIVE and states its age instead.
 */

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("LiveBadge", () => {
  it("renders the word and a moving dot while live", () => {
    const { container } = render(<LiveBadge live />);
    expect(container.textContent).toBe("Live");
    expect(container.querySelector(".live-dot.is-live")).toBeTruthy();
  });

  it("renders nothing at all when not live", () => {
    const { container } = render(<LiveBadge live={false} />);
    expect(container.innerHTML).toBe("");
  });
});

describe("ScanAge", () => {
  it("states the age of the scan and never claims LIVE", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-26T12:05:00"));
    const { container } = render(<ScanAge capturedAt="2026-09-26T12:02:00" />);
    expect(container.textContent).toBe("Scanned 3m ago");
    expect(container.querySelector(".live-dot")).toBeNull();
  });

  it("moves the age on while the card stays open", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-26T12:02:10"));
    const { container } = render(<ScanAge capturedAt="2026-09-26T12:02:00" />);
    expect(container.textContent).toBe("Scanned just now");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(container.textContent).toBe("Scanned 1m ago");
  });

  it("renders nothing before the first scan", () => {
    const { container } = render(<ScanAge capturedAt={undefined} />);
    expect(container.innerHTML).toBe("");
  });
});
