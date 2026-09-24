// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountMenu from "./AccountMenu";

/**
 * The phone toolbar's account menu, and the ways out of it.
 *
 * Layout is CSS and jsdom does not apply it, so these check behaviour only:
 * the toggle reports its state, and the menu closes on a tap elsewhere and on
 * Esc. The tap matters most -- iOS Safari does not focus a tapped button, so a
 * menu that waited for a blur would stay open over the page there.
 */

afterEach(cleanup);

function renderMenu(onLogout = vi.fn()) {
  render(
    <div>
      <p>outside</p>
      <AccountMenu
        displayName="Bench User"
        username="bench"
        role="admin"
        eventAge="last event 12m ago"
        onLogout={onLogout}
      />
    </div>,
  );
  const toggle = screen.getByRole("button", { name: "Account: Bench User" });
  const root = toggle.closest(".auth-chip")!;
  return { toggle, root, onLogout };
}

describe("AccountMenu", () => {
  it("opens on the toggle and says so", () => {
    const { toggle, root } = renderMenu();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(root.classList.contains("open")).toBe(true);
  });

  it("closes on a tap outside, not on one inside", () => {
    const { toggle, root } = renderMenu();
    fireEvent.click(toggle);
    fireEvent.pointerDown(screen.getByText("Bench User"));
    expect(root.classList.contains("open")).toBe(true);
    fireEvent.pointerDown(screen.getByText("outside"));
    expect(root.classList.contains("open")).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes on Esc and hands focus back to the toggle", () => {
    const { toggle, root } = renderMenu();
    fireEvent.click(toggle);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(root.classList.contains("open")).toBe(false);
    expect(document.activeElement).toBe(toggle);
  });

  it("keeps one Logout, which logs out and closes the menu", () => {
    const { toggle, root, onLogout } = renderMenu();
    fireEvent.click(toggle);
    const logout = screen.getAllByRole("button", { name: "Logout" });
    expect(logout).toHaveLength(1);
    fireEvent.click(logout[0]);
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(root.classList.contains("open")).toBe(false);
  });
});
