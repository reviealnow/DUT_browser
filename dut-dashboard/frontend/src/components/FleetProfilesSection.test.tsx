// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FleetProfile } from "../api/rest";

/**
 * What the Profiles table lets somebody do to somebody else's saved setting.
 *
 * The page is small and its two interesting claims are both social rather than
 * technical. A *shared* profile is visible to the bench and editable only by
 * its author, so the row of a colleague must offer no pencil at all — a greyed
 * one invites a click that can only answer 403, and the reason it would is a
 * fact the Owner column already states. And nothing here may ever carry a
 * password: profiles exist precisely so that the address and the login name can
 * be saved while the password stays in the backend's memory.
 */

const getFleetProfiles = vi.fn();
const updateFleetProfile = vi.fn();
const deleteFleetProfile = vi.fn();

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getFleetProfiles: () => getFleetProfiles(),
  updateFleetProfile: (...args: unknown[]) => updateFleetProfile(...args),
  deleteFleetProfile: (...args: unknown[]) => deleteFleetProfile(...args),
}));

const { default: FleetProfilesSection } = await import("./FleetProfilesSection");

function profile(over: Partial<FleetProfile> = {}): FleetProfile {
  return {
    id: 1,
    name: "gavin-bench",
    device_name: "Sniffer Host 1",
    host: "192.168.68.63",
    port: 22,
    username: "gavin",
    scope: "shared",
    owner: "Gavin",
    owner_user_id: 4,
    can_edit: true,
    created_at: "2026-09-12T09:10:30",
    updated_at: "2026-09-12T09:10:30",
    ...over,
  };
}

async function show(rows: FleetProfile[]) {
  getFleetProfiles.mockResolvedValue(rows);
  render(<FleetProfilesSection />);
  if (rows.length) {
    await screen.findByText(rows[0].host);
  } else {
    await screen.findByText("No profiles saved yet.");
  }
}

const button = (label: string) =>
  screen.queryAllByRole("button").find((element) => element.textContent === label);

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(cleanup);

describe("whose profile it is", () => {
  it("offers no edit or delete on a row the viewer does not own", async () => {
    await show([profile({ can_edit: false, owner: "Amy" })]);
    expect(screen.queryByLabelText("Edit gavin-bench")).toBeNull();
    expect(screen.queryByLabelText("Delete gavin-bench")).toBeNull();
    // Said in words instead, so the absence reads as a rule rather than a bug.
    expect(screen.getByText("Amy’s")).toBeTruthy();
  });

  it("offers both on the viewer's own", async () => {
    await show([profile({ can_edit: true })]);
    expect(screen.getByLabelText("Edit gavin-bench")).toBeTruthy();
    expect(screen.getByLabelText("Delete gavin-bench")).toBeTruthy();
  });

  it("says which rows the bench can see", async () => {
    await show([
      profile({ id: 1, name: "shared-one" }),
      profile({ id: 2, name: "mine", host: "10.0.0.7", scope: "private" }),
    ]);
    expect(screen.getByText("Shared")).toBeTruthy();
    expect(screen.getByText("Private")).toBeTruthy();
  });
});

describe("editing a row", () => {
  it("sends every field, including the scope, and never a password", async () => {
    updateFleetProfile.mockResolvedValue(profile());
    await show([profile()]);
    screen.getByLabelText("Edit gavin-bench").click();

    fireEvent.change(await screen.findByLabelText("Host"), { target: { value: "10.0.0.7" } });
    fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "private" } });
    button("Save")!.click();

    await waitFor(() => expect(updateFleetProfile).toHaveBeenCalled());
    const [id, sent] = updateFleetProfile.mock.calls[0];
    expect(id).toBe(1);
    expect(sent).toEqual({
      name: "gavin-bench",
      device_name: "Sniffer Host 1",
      host: "10.0.0.7",
      port: 22,
      username: "gavin",
      scope: "private",
    });
    expect(Object.keys(sent)).not.toContain("password");
  });

  it("asks before deleting, and says the hosts stay", async () => {
    deleteFleetProfile.mockResolvedValue(undefined);
    await show([profile()]);
    screen.getByLabelText("Delete gavin-bench").click();

    await waitFor(() => expect(deleteFleetProfile).toHaveBeenCalledWith(1));
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toMatch(/not touched/);
  });

  it("keeps the row when the confirmation is declined", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    await show([profile()]);
    screen.getByLabelText("Delete gavin-bench").click();
    expect(deleteFleetProfile).not.toHaveBeenCalled();
  });
});

describe("an empty drawer", () => {
  it("says where profiles come from rather than only that there are none", async () => {
    await show([]);
    expect(screen.getByText(/Save one from a card on the Hosts page/)).toBeTruthy();
  });
});
