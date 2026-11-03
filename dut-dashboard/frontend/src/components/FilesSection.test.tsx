// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FilesList } from "../api/rest";

/**
 * The Filename column in Shared Files is cut to a width the user drags at its
 * header divider, as in Finder's list view. A long name used to push Size,
 * Uploader, SHA-256 and the row actions off the card.
 */

const getFiles = vi.fn();

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getFiles: () => getFiles(),
}));

const { default: FilesSection } = await import("./FilesSection");

const LONG = "BFID0928_Rack_Information_with_a_very_long_descriptive_name.pdf";

const LISTING: FilesList = {
  files: [{ id: 1, filename: LONG, size: 4_200_000, uploader: "nelson", uploaded_at: "2026-11-03T09:48:00", sha256: "5472d7590716" }],
  stats: { total: 1, total_size: 4_200_000, contributors: 1, this_week: 1, uploads_per_day: [], files_by_type: [], top_uploaders: [] },
  total: 1,
};

async function nameCell() {
  render(<FilesSection query="" />);
  return (await screen.findByTitle(LONG)) as HTMLElement;
}

beforeEach(() => {
  localStorage.clear();
  getFiles.mockResolvedValue(LISTING);
});

afterEach(() => {
  cleanup();
  getFiles.mockReset();
});

describe("the Shared Files Filename column", () => {
  it("cuts the name to the default width and keeps the full name on hover", async () => {
    const name = await nameCell();
    expect(name.textContent).toBe(LONG);
    expect(name.style.width).toBe("220px");
  });

  it("resizes when the header divider is dragged, and remembers it", async () => {
    const name = await nameCell();
    const divider = screen.getByRole("separator", { name: "Resize Filename column" });
    divider.setPointerCapture = () => {};

    fireEvent.pointerDown(divider, { clientX: 300, pointerId: 1 });
    fireEvent.pointerMove(divider, { clientX: 400, pointerId: 1 });
    fireEvent.pointerUp(divider, { clientX: 400, pointerId: 1 });

    expect(name.style.width).toBe("320px");
    expect(localStorage.getItem("dut.files.nameWidth.v1")).toBe("320");
  });

  it("nudges with the arrow keys, clamps, and resets on double-click", async () => {
    const name = await nameCell();
    const divider = screen.getByRole("separator", { name: "Resize Filename column" });

    fireEvent.keyDown(divider, { key: "ArrowLeft" });
    expect(name.style.width).toBe("204px");

    for (let i = 0; i < 20; i++) fireEvent.keyDown(divider, { key: "ArrowLeft" });
    expect(name.style.width).toBe("80px");

    fireEvent.doubleClick(divider);
    expect(name.style.width).toBe("220px");
  });
});
