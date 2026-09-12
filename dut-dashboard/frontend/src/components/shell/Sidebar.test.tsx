// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Role } from "../../api/rest";

/**
 * What the sidebar still lets a human reach once it has been collapsed.
 *
 * A collapsed rail is a navigation that has hidden its own labels, so the ways
 * back to a section are the interesting part, not the width. Three of them can
 * break silently and none of them shows up in a screenshot:
 *
 *  - a rail that renders group buttons but never opens them is a dead end —
 *    every section below the first is unreachable;
 *  - hover-only disclosure looks fine on the developer's mouse and locks out
 *    the keyboard entirely, so the button has to answer click and focus too;
 *  - the role filter has to survive the second layer. A guest sees no System
 *    section at all, and a rail button that opens onto an empty panel is worse
 *    than the header this repo already took care not to orphan.
 *
 * The drawer case is here for the same reason: rail and drawer are two answers
 * to the same shortage of width, and applying both would slide a 64px strip of
 * unlabelled icons over the page.
 */

let role: Role = "admin";

vi.mock("../../monitoring/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../monitoring/AuthContext")>()),
  useAuth: () => ({ role }),
}));

const { default: Sidebar } = await import("./Sidebar");

/** jsdom's matchMedia never evaluates the query; the width is ours to state. */
function setViewport(mobile: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: mobile,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

function renderSidebar(props: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const onSelect = vi.fn();
  const onToggleCollapsed = vi.fn();
  render(
    <Sidebar
      active="overview"
      onSelect={onSelect}
      open={false}
      onClose={vi.fn()}
      collapsed={false}
      onToggleCollapsed={onToggleCollapsed}
      {...props}
    />,
  );
  return { onSelect, onToggleCollapsed };
}

beforeEach(() => {
  role = "admin";
  setViewport(false);
});

afterEach(cleanup);

describe("expanded", () => {
  it("lists every section under its group header", () => {
    renderSidebar();
    expect(screen.getByText("Monitoring")).toBeDefined();
    expect(screen.getByRole("button", { name: /Serial Console/ })).toBeDefined();
    expect(screen.getByRole("button", { name: /Upgrade Firmware/ })).toBeDefined();
  });

  it("offers a collapse control", () => {
    const { onToggleCollapsed } = renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Collapse navigation" }));
    expect(onToggleCollapsed).toHaveBeenCalledTimes(1);
  });
});

describe("collapsed to a rail", () => {
  it("hides the section labels behind one button per group", () => {
    renderSidebar({ collapsed: true });
    expect(screen.getByRole("button", { name: "Monitoring" })).toBeDefined();
    // The sections are still in the DOM, but hidden — nothing reads them out
    // and nothing can tab to them until the group is opened.
    expect(screen.queryByRole("button", { name: /Serial Console/ })).toBeNull();
  });

  it("opens a group on click and closes it again", () => {
    renderSidebar({ collapsed: true });
    const group = screen.getByRole("button", { name: "Monitoring" });
    expect(group.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(group);
    expect(group.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: /Serial Console/ })).toBeDefined();
    fireEvent.click(group);
    expect(screen.queryByRole("button", { name: /Serial Console/ })).toBeNull();
  });

  it("opens a group on keyboard focus, and Esc closes it", () => {
    renderSidebar({ collapsed: true });
    const group = screen.getByRole("button", { name: "Workspace" });
    fireEvent.focus(group);
    expect(screen.getByRole("button", { name: /Bulletin/ })).toBeDefined();
    fireEvent.keyDown(group, { key: "Escape" });
    expect(screen.queryByRole("button", { name: /Bulletin/ })).toBeNull();
  });

  it("selects a section from the flyout and closes it", () => {
    const { onSelect } = renderSidebar({ collapsed: true });
    fireEvent.click(screen.getByRole("button", { name: "Monitoring" }));
    fireEvent.click(screen.getByRole("button", { name: /Site Survey/ }));
    expect(onSelect).toHaveBeenCalledWith("sitesurvey");
    expect(screen.queryByRole("button", { name: /Site Survey/ })).toBeNull();
  });

  it("marks the group holding the active section", () => {
    renderSidebar({ collapsed: true, active: "files" });
    expect(screen.getByRole("button", { name: "Workspace" }).className).toContain("active");
    expect(screen.getByRole("button", { name: "Monitoring" }).className).not.toContain("active");
  });

  it("drops a group the role cannot see anything in", () => {
    role = "guest";
    renderSidebar({ collapsed: true });
    expect(screen.getByRole("button", { name: "Monitoring" })).toBeDefined();
    // Settings is engineer, Upgrade Firmware is admin: System is empty for a
    // guest, so there is no button that opens onto nothing.
    expect(screen.queryByRole("button", { name: "System" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Workspace" })).toBeNull();
  });
});

describe("under the drawer breakpoint", () => {
  it("ignores a remembered rail and shows the full labels", () => {
    setViewport(true);
    renderSidebar({ collapsed: true, open: true });
    expect(screen.getByRole("button", { name: /Serial Console/ })).toBeDefined();
    expect(screen.queryByRole("button", { name: "Monitoring" })).toBeNull();
  });
});
