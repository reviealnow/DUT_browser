// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CollectorConsoles, CollectorStatus, Role } from "../api/rest";

/**
 * What the collectors card tells an admin, and what it refuses to imply.
 *
 * Two claims here are worth a test each because getting them wrong is silent.
 *
 * The breathing light means "an SSH session is being held right now". If it
 * ever breathes on a collector that is merely *registered*, the feature has
 * inverted: the whole reason the dot moves is that a static green one reads the
 * same whether the session is alive or died ten minutes ago.
 *
 * The password is memory-only in the backend, so `has_password` is false after
 * every restart. A row in that state must ask for the password rather than
 * offering Connect — a button that can only answer 400 teaches people the
 * feature is broken.
 */

let role: Role = "admin";
vi.mock("../monitoring/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../monitoring/AuthContext")>()),
  useAuth: () => ({ role }),
}));

const getCollectors = vi.fn();
const connectCollector = vi.fn();
const disconnectCollector = vi.fn();
const setCollectorPassword = vi.fn();
const configureCollector = vi.fn();
const removeCollector = vi.fn();
const getCollectorConsoles = vi.fn();
const attachCollectorConsole = vi.fn();
const detachCollectorConsole = vi.fn();

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getCollectors: () => getCollectors(),
  connectCollector: (...args: unknown[]) => connectCollector(...args),
  disconnectCollector: (...args: unknown[]) => disconnectCollector(...args),
  setCollectorPassword: (...args: unknown[]) => setCollectorPassword(...args),
  configureCollector: (...args: unknown[]) => configureCollector(...args),
  removeCollector: (...args: unknown[]) => removeCollector(...args),
  getCollectorConsoles: (...args: unknown[]) => getCollectorConsoles(...args),
  attachCollectorConsole: (...args: unknown[]) => attachCollectorConsole(...args),
  detachCollectorConsole: (...args: unknown[]) => detachCollectorConsole(...args),
}));

const { default: EdgeCollectorsCard } = await import("./EdgeCollectorsCard");

function collector(over: Partial<CollectorStatus> = {}): CollectorStatus {
  return {
    id: "edge1",
    label: "Edge collector (lab)",
    ip: "10.0.0.9",
    hostname: "edge-collector",
    user: "dut",
    port: 22,
    auth: "password",
    key_path: null,
    connected: false,
    ready: true,
    has_password: true,
    reported_hostname: null,
    connected_since: null,
    detail: null,
    ...over,
  };
}

async function show(rows: CollectorStatus[]) {
  getCollectors.mockResolvedValue(rows);
  render(<EdgeCollectorsCard />);
  if (rows.length) {
    await screen.findByText(rows[0].label);
  } else {
    await screen.findByText("Nothing registered yet.");
  }
}

const dot = () => document.querySelector(".collector-row .live-dot");
const button = (label: string) =>
  screen.queryAllByRole("button").find((element) => element.textContent === label);

function consoles(over: Partial<CollectorConsoles> = {}): CollectorConsoles {
  return {
    collector: "edge1",
    hostname: "raspberrypi",
    socat: { present: true, path: "/usr/bin/socat" },
    serial_group: { name: "dialout", member: true, groups: ["dut", "dialout"] },
    busy_check: "fuser",
    devices: [],
    blockers: [],
    ...over,
  };
}

function device(over: Partial<CollectorConsoles["devices"][number]> = {}) {
  return { device: "/dev/ttyUSB0", busy: false, held_by: null, attached_dut: null, ...over };
}

beforeEach(() => {
  role = "admin";
  vi.clearAllMocks();
  getCollectorConsoles.mockResolvedValue(consoles());
});

afterEach(cleanup);

describe("what the light claims", () => {
  it("breathes only while a session is actually held", async () => {
    await show([collector({ connected: true, connected_since: "11:02:33" })]);
    expect(dot()?.classList.contains("is-live")).toBe(true);
    expect(screen.getByText("Connected")).toBeTruthy();
  });

  it("rests for a collector that is registered but not logged in", async () => {
    await show([collector({ connected: false })]);
    expect(dot()).toBeTruthy();
    expect(dot()?.classList.contains("is-live")).toBe(false);
    expect(screen.getByText("Not connected")).toBeTruthy();
  });

  it("says it in words too, for anyone the motion never reaches", async () => {
    await show([collector({ connected: true })]);
    expect(dot()?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByText("Connected")).toBeTruthy();
  });
});

describe("a password the backend has forgotten", () => {
  it("asks for it instead of offering a Connect that can only fail", async () => {
    await show([collector({ has_password: false, ready: false })]);
    expect(button("Connect")).toBeUndefined();
    expect(screen.getByLabelText("SSH password for Edge collector (lab)")).toBeTruthy();
  });

  it("offers Connect once a password is held", async () => {
    await show([collector({ has_password: true })]);
    expect(button("Connect")).toBeTruthy();
    expect(screen.queryByLabelText("SSH password for Edge collector (lab)")).toBeNull();
  });

  it("never renders a password as readable text", async () => {
    // Not a hypothetical: the field is beside an address and a login name that
    // are shown in the clear, and the difference between those and this one is
    // the entire security posture of the card.
    await show([collector({ has_password: false, ready: false })]);
    const field = screen.getByLabelText("SSH password for Edge collector (lab)");
    expect(field.getAttribute("type")).toBe("password");
    expect(field.getAttribute("autocomplete")).toBe("off");
  });
});

describe("a collector that an existing remote node became", () => {
  /**
   * The merge, from the card's side. A node that was registered with a key is
   * now a collector like any other — and must never be shown a field asking for
   * a password it does not use, or a Connect button gated on one it will never
   * have.
   */
  const keyed = () =>
    collector({
      auth: "key" as const,
      key_path: "/home/you/.ssh/dut_fleet_ed25519",
      // No password is held and none is needed. These two answering differently
      // is the entire point of `ready` existing beside `has_password`.
      has_password: false,
      ready: true,
      hostname: null,
    });

  it("offers Connect without ever asking for a password", async () => {
    await show([keyed()]);
    expect(button("Connect")).toBeTruthy();
    expect(screen.queryByLabelText(/SSH password for/)).toBeNull();
  });

  it("names the key file, which is what somebody checks when a login fails", async () => {
    await show([keyed()]);
    expect(screen.getByText("/home/you/.ssh/dut_fleet_ed25519")).toBeTruthy();
  });

  it("claims no expected hostname when nobody recorded one", async () => {
    // Migrated nodes carry nobody's expectation. Printing one would either
    // manufacture a mismatch or hide a real one.
    await show([keyed()]);
    expect(screen.queryByText(/expects/)).toBeNull();
  });

  it("still asks a password collector for its password", async () => {
    // The other side of the same gate, so `ready` cannot quietly become "true
    // for everything".
    await show([collector({ auth: "password", has_password: false, ready: false })]);
    expect(button("Connect")).toBeUndefined();
    expect(screen.getByLabelText(/SSH password for/)).toBeTruthy();
  });
});

describe("logging in to something that is not what was registered", () => {
  it("reports the name the box gave, without calling the login a failure", async () => {
    connectCollector.mockResolvedValue({
      ok: true,
      ...collector({ connected: true }),
      hostname_matches: false,
      reported_hostname: "some-other-pi",
    });
    await show([collector({ has_password: true })]);
    button("Connect")!.click();
    await waitFor(() =>
      expect(screen.getByText(/calls itself "some-other-pi"/)).toBeTruthy(),
    );
  });
});

describe("the DUT consoles behind a collector", () => {
  async function showConnected(listing: CollectorConsoles) {
    getCollectorConsoles.mockResolvedValue(listing);
    await show([collector({ connected: true, reported_hostname: "raspberrypi" })]);
    await screen.findByText(/DUT consoles on raspberrypi/);
  }

  it("only asks the collector once the session is up", async () => {
    // Every scan is four commands on somebody's Pi. A card that scans a
    // collector nobody has connected is asking a question it cannot ask.
    await show([collector({ connected: false })]);
    expect(getCollectorConsoles).not.toHaveBeenCalled();
  });

  it("tells free, in use, and nobody looked apart", async () => {
    // The third is the one that matters: reporting an unchecked port as free is
    // how a bench loses an afternoon to a minicom from yesterday.
    await showConnected(
      consoles({
        devices: [
          device({ device: "/dev/ttyUSB0", busy: false }),
          device({ device: "/dev/ttyUSB1", busy: true, held_by: "2043" }),
          device({ device: "/dev/ttyACM0", busy: null }),
        ],
      }),
    );
    expect(screen.getByText("Free")).toBeTruthy();
    expect(screen.getByText(/In use on the collector by pid 2043/)).toBeTruthy();
    expect(screen.getByText("In use? Not checked")).toBeTruthy();
  });

  it("distinguishes a port this dashboard holds from one the box says is busy", async () => {
    await showConnected(
      consoles({ devices: [device({ attached_dut: "edge1-ttyusb0", busy: true })] }),
    );
    expect(screen.getByText("Attached here as edge1-ttyusb0")).toBeTruthy();
    expect(button("Detach")).toBeTruthy();
    expect(button("Attach")).toBeUndefined();
  });

  it("offers Attach on a port nothing is holding", async () => {
    await showConnected(consoles({ devices: [device()] }));
    expect(button("Attach")).toBeTruthy();
    expect(button("Detach")).toBeUndefined();
  });

  it("attaches at the baud rate that is selected", async () => {
    attachCollectorConsole.mockResolvedValue({ dut: "edge1-ttyusb0", device: "/dev/ttyUSB0" });
    await showConnected(consoles({ devices: [device()] }));
    button("Attach")!.click();
    await waitFor(() =>
      expect(attachCollectorConsole).toHaveBeenCalledWith("edge1", "/dev/ttyUSB0", 115200, {
        is_mesh: false,
        backhaul_iface: null,
      }),
    );
  });

  it("declares a mesh node per port, not per panel", async () => {
    /**
     * Whether a DUT is in a mesh is a fact about that DUT. One toggle governing
     * whichever port you press next is how the wrong console ends up declared
     * meshed -- and a wrongly meshed DUT does not fail, it reports a backhaul
     * capture that is simply untrue.
     *
     * These two fields are the whole reason retiring the node-registration form
     * needed work rather than a deletion: nothing else in the UI can say them.
     */
    attachCollectorConsole.mockResolvedValue({ dut: "edge1-ttyusb1", device: "/dev/ttyUSB1" });
    await showConnected(
      consoles({
        devices: [device({ device: "/dev/ttyUSB0" }), device({ device: "/dev/ttyUSB1" })],
      }),
    );
    (screen.getByLabelText("/dev/ttyUSB1 is a mesh node") as HTMLInputElement).click();
    const iface = await screen.findByLabelText("Backhaul interface for /dev/ttyUSB1");
    fireEvent.change(iface, { target: { value: "ath16" } });
    // The other port is untouched: no interface field appeared for it.
    expect(screen.queryByLabelText("Backhaul interface for /dev/ttyUSB0")).toBeNull();

    screen.getAllByRole("button").filter((b) => b.textContent === "Attach")[1].click();
    await waitFor(() =>
      expect(attachCollectorConsole).toHaveBeenCalledWith("edge1", "/dev/ttyUSB1", 115200, {
        is_mesh: true,
        backhaul_iface: "ath16",
      }),
    );
  });

  it("says what stands between the collector and a console", async () => {
    await showConnected(
      consoles({
        socat: { present: false, path: null },
        blockers: ["socat is not installed on this collector (apt install socat)."],
      }),
    );
    expect(screen.getByText(/socat is not installed/)).toBeTruthy();
  });

  it("says when nothing could check whether a port is busy", async () => {
    await showConnected(consoles({ busy_check: "unavailable", devices: [device({ busy: null })] }));
    // Matched on a contiguous fragment: the sentence is broken up by <code>
    // elements, so a regex spanning them matches no single node.
    expect(screen.getByText(/nothing could check whether a port/)).toBeTruthy();
  });
});

describe("who sees this at all", () => {
  it("draws nothing for an engineer", async () => {
    // Every /api/collectors route is admin, so a form here could only 403.
    role = "engineer";
    const { container } = render(<EdgeCollectorsCard />);
    expect(container.firstChild).toBeNull();
    expect(getCollectors).not.toHaveBeenCalled();
  });
});
