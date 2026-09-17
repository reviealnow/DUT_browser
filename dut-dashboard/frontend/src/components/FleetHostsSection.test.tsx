// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MAX_COLLECTORS } from "../api/rest";
import type { CollectorConsoles, CollectorStatus, FleetProfile, Role } from "../api/rest";

/**
 * What the Hosts page tells an admin, and what it refuses to imply.
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
const getCollectorHostKey = vi.fn();
const trustCollectorHostKey = vi.fn();
const getFleetProfiles = vi.fn();
const createFleetProfile = vi.fn();
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
  getCollectorHostKey: (...args: unknown[]) => getCollectorHostKey(...args),
  trustCollectorHostKey: (...args: unknown[]) => trustCollectorHostKey(...args),
  getFleetProfiles: () => getFleetProfiles(),
  createFleetProfile: (...args: unknown[]) => createFleetProfile(...args),
  attachCollectorConsole: (...args: unknown[]) => attachCollectorConsole(...args),
  detachCollectorConsole: (...args: unknown[]) => detachCollectorConsole(...args),
}));

const { default: FleetHostsSection } = await import("./FleetHostsSection");

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
  render(<FleetHostsSection onManageProfiles={manageProfiles} />);
  if (rows.length) {
    await screen.findByLabelText(`Name for ${rows[0].id}`);
  } else {
    await screen.findByText("Nothing registered yet.");
  }
}

const dot = () => document.querySelector(".host-card .live-dot");
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
  return {
    device: "/dev/ttyUSB0", busy: false, held_by: null,
    attached_dut: null, registered_dut: null, ...over,
  };
}

const manageProfiles = vi.fn();

beforeEach(() => {
  role = "admin";
  vi.clearAllMocks();
  getCollectorConsoles.mockResolvedValue(consoles());
  getCollectorHostKey.mockResolvedValue({
    host: "10.0.0.9", port: 22, known: true, known_keys: [], presented_keys: [],
    matches: true, scan_error: null,
  });
  getFleetProfiles.mockResolvedValue([]);
  configureCollector.mockResolvedValue(undefined);
  connectCollector.mockResolvedValue({ ok: true, hostname_matches: true });
});

afterEach(cleanup);

describe("what the light claims", () => {
  it("breathes only while a session is actually held", async () => {
    await show([collector({ connected: true, connected_since: "11:02:33" })]);
    expect(dot()?.classList.contains("is-live")).toBe(true);
    expect(screen.getByText("Connected")).toBeTruthy();
  });

  it("says Ready for a host that could log in but has not", async () => {
    // The footer's own question, which the dot does not answer: this host is
    // one press away, as opposed to one password away.
    await show([collector({ connected: false, ready: true })]);
    expect(screen.getByText("Ready")).toBeTruthy();
  });

  it("rests for a collector that is registered but not logged in", async () => {
    await show([collector({ connected: false })]);
    expect(dot()).toBeTruthy();
    expect(dot()?.classList.contains("is-live")).toBe(false);
    expect(screen.getByText("Not connected")).toBeTruthy();
  });

  it("says it in words too, for anyone the motion never reaches", async () => {
    await show([collector({ connected: true, connected_since: "11:02:33" })]);
    expect(dot()?.getAttribute("aria-hidden")).toBe("true");
    // The word by the name is the claim; the footer says since when. Neither
    // is the other's duplicate, and the motion is a third telling of the same
    // fact for whoever is across the room.
    expect(screen.getByText("Connected")).toBeTruthy();
    expect(screen.getByText("Since 11:02:33")).toBeTruthy();
  });
});

describe("a password the backend has forgotten", () => {
  it("refuses to try a login it has no password for", async () => {
    // Memory-only passwords mean `has_password` is false after every restart.
    // A Verify that can only answer 400 teaches people the feature is broken,
    // so the button is held shut and the footer says which of the two things is
    // missing — the network, or the password.
    await show([collector({ has_password: false, ready: false })]);
    expect(button("Verify")?.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Needs its password again")).toBeTruthy();
  });

  it("opens as soon as one is typed, without a save in between", async () => {
    await show([collector({ has_password: false, ready: false })]);
    fireEvent.change(screen.getByLabelText("SSH password for edge1"), {
      target: { value: "hunter2" },
    });
    expect(button("Verify")?.hasAttribute("disabled")).toBe(false);

    button("Verify")!.click();
    await waitFor(() => expect(connectCollector).toHaveBeenCalledWith("edge1"));
    // Written with the rest of the fields rather than through a second
    // endpoint: one press is the whole transaction the card offers.
    expect(configureCollector).toHaveBeenCalledWith(
      expect.objectContaining({ id: "edge1", password: "hunter2" }),
    );
  });

  it("sends no password at all for a host whose one is still held", async () => {
    // Absent means "keep what is in memory". Sending an empty string would
    // replace a working login with a blank one.
    await show([collector({ has_password: true })]);
    button("Verify")!.click();
    await waitFor(() => expect(configureCollector).toHaveBeenCalled());
    expect(configureCollector.mock.calls[0][0]).not.toHaveProperty("password");
  });

  it("never renders a password as readable text", async () => {
    // Not a hypothetical: the field sits in a row with an address and a login
    // name that are shown in the clear, and the difference between those and
    // this one is the entire security posture of the page.
    await show([collector({ has_password: false, ready: false })]);
    const field = screen.getByLabelText("SSH password for edge1");
    expect(field.getAttribute("type")).toBe("password");
    expect(field.getAttribute("autocomplete")).toBe("off");
  });

  it("never fills the field from the backend, which could not answer it anyway", async () => {
    await show([collector({ has_password: true })]);
    const field = screen.getByLabelText(
      "SSH password for edge1",
    ) as HTMLInputElement;
    expect(field.value).toBe("");
    // The placeholder is how a held password is reported — the only honest way
    // to show one this side has never seen.
    expect(field.getAttribute("placeholder")).toBe("held in memory");
  });
});

describe("what the footer says after an attempt that left no session", () => {
  /**
   * Reported from the bench: a host whose login failed on an unknown host key
   * read "Ready · The host key is not known to this machine…" — two claims that
   * contradict each other. The first says press the button; the second says
   * pressing it changes nothing until somebody accepts a key by hand.
   *
   * `ready` stays true through a failed login, because it means "a password is
   * held", not "the last attempt worked". So the detail has to win.
   */
  const KEY = "The host key is not known to this machine. SSH to it by hand once, "
    + "check the fingerprint, then retry.";

  it("says what happened, not that the host is ready", async () => {
    await show([collector({ connected: false, ready: true, detail: KEY })]);
    expect(screen.getByText(KEY)).toBeTruthy();
    expect(screen.queryByText("Ready")).toBeNull();
  });

  it("says it exactly once", async () => {
    // It used to be the status AND the sentence appended to it; a reader would
    // have seen the reason twice on one line.
    await show([collector({ connected: false, ready: true, detail: KEY })]);
    expect(screen.getAllByText(KEY)).toHaveLength(1);
  });

  it("still says Ready when nothing has gone wrong", async () => {
    await show([collector({ connected: false, ready: true, detail: null })]);
    expect(screen.getByText("Ready")).toBeTruthy();
  });

  it("keeps the password sentence ahead of an older failure", async () => {
    // A restart forgets the password and keeps whatever detail was there. The
    // actionable thing is the password, so that stays the status — and the
    // older reason is still printed beside it rather than dropped.
    await show([collector({ connected: false, ready: false, has_password: false, detail: KEY })]);
    expect(screen.getByText("Needs its password again")).toBeTruthy();
    expect(screen.getByText(`· ${KEY}`)).toBeTruthy();
  });

  it("keeps a connected host's detail beside the time, not instead of it", async () => {
    // The other kind of detail: the login worked, and the box answered to a
    // name nobody registered. That is not a status, it is a footnote to one.
    const mismatch = 'Logged in, but the box calls itself "some-other-pi", not "edge-collector".';
    await show([collector({ connected: true, connected_since: "11:02:33", detail: mismatch })]);
    expect(screen.getByText("Since 11:02:33")).toBeTruthy();
    expect(screen.getByText(`· ${mismatch}`)).toBeTruthy();
  });
});

describe("a host that an existing remote node became", () => {
  /**
   * The merge, from the page's side. A node that was registered with a key is
   * now a host like any other — and must never be shown a field asking for a
   * password it does not use, or a Verify held shut waiting for one.
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

  it("offers Verify without ever asking for a password", async () => {
    await show([keyed()]);
    expect(button("Verify")?.hasAttribute("disabled")).toBe(false);
    expect(screen.queryByLabelText(/SSH password for/)).toBeNull();
  });

  it("names the key file, which is what somebody checks when a login fails", async () => {
    await show([keyed()]);
    const field = screen.getByLabelText(
      "Private key path for edge1",
    ) as HTMLInputElement;
    expect(field.value).toBe("/home/you/.ssh/dut_fleet_ed25519");
  });

  it("claims no expected hostname when nobody recorded one", async () => {
    // Migrated nodes carry nobody's expectation. Printing one would either
    // manufacture a mismatch or hide a real one — and re-saving the card must
    // not invent one either.
    await show([keyed()]);
    button("Details")!.click();
    const field = (await screen.findByLabelText(
      "Expected hostname for edge1",
    )) as HTMLInputElement;
    expect(field.value).toBe("");

    button("Verify")!.click();
    await waitFor(() => expect(configureCollector).toHaveBeenCalled());
    expect(configureCollector.mock.calls[0][0].hostname).toBeNull();
  });

  it("still holds a password host shut until it has one", async () => {
    // The other side of the same gate, so `ready` cannot quietly become "true
    // for everything".
    await show([collector({ auth: "password", has_password: false, ready: false })]);
    expect(button("Verify")?.hasAttribute("disabled")).toBe(true);
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
    button("Verify")!.click();
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

  it("offers Attach again on a port whose console has closed", async () => {
    /**
     * The state the bench got stuck in: `attached` used to be computed from the
     * registration alone, and Detach leaves that standing on purpose — the DUT
     * keeps its history, its label and its settings. So the row claimed a
     * session that had ended, the button stayed Detach, and pressing it changed
     * nothing anyone could see. There was no way back to Attach at all.
     */
    await showConnected(
      consoles({ devices: [device({ attached_dut: null, registered_dut: "edge1-ttyusb0" })] }),
    );
    expect(button("Attach")).toBeTruthy();
    expect(button("Detach")).toBeUndefined();
    // And it says which DUT that attach will land on, rather than reading as a
    // port nobody has touched.
    expect(screen.getByText(/Registered as edge1-ttyusb0 · console closed/)).toBeTruthy();
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

describe("adding a host nobody has registered", () => {
  const connects = () =>
    screen.getAllByRole("button").filter((element) => element.textContent === "Verify");

  async function addCard(): Promise<void> {
    button("Add new host")!.click();
    await screen.findByLabelText("Name for new host 1");
  }

  function fill(name: string, host: string, user: string, password: string): void {
    fireEvent.change(screen.getByLabelText("Name for new host 1"), {
      target: { value: name },
    });
    fireEvent.change(screen.getByLabelText("Address for new host 1"), { target: { value: host } });
    fireEvent.change(screen.getByLabelText("SSH user for new host 1"), { target: { value: user } });
    fireEvent.change(screen.getByLabelText("SSH password for new host 1"), {
      target: { value: password },
    });
  }

  it("derives an id that cannot land on one already registered", async () => {
    /**
     * Re-posting an id is an *edit* in the registry, so a derived id that
     * collides would silently re-point somebody else's registration at a
     * different machine. Two boxes an operator happens to name the same are
     * two cards, not one overwritten one.
     */
    await show([collector({ id: "lab-pi", label: "Lab Pi" })]);
    await addCard();
    fill("Lab Pi", "10.0.0.30", "pi", "hunter2");

    connects()[1].click();
    await waitFor(() => expect(configureCollector).toHaveBeenCalled());
    expect(configureCollector.mock.calls[0][0]).toMatchObject({
      id: "lab-pi-2",
      ip: "10.0.0.30",
      user: "pi",
      password: "hunter2",
    });
  });

  it("refuses to add one past the limit rather than failing at the server", async () => {
    await show(
      Array.from({ length: MAX_COLLECTORS }, (_unused, index) =>
        collector({ id: `edge${index}`, label: `Edge ${index}` }),
      ),
    );
    expect(button("Add new host")?.hasAttribute("disabled")).toBe(true);
  });
});

describe("a saved profile", () => {
  const profile = (over: Partial<FleetProfile> = {}): FleetProfile => ({
    id: 7,
    name: "bench-pi",
    device_name: "Bench Pi",
    host: "192.168.68.63",
    port: 2222,
    username: "gavin",
    scope: "shared",
    owner: "Gavin",
    owner_user_id: 1,
    can_edit: false,
    created_at: "2026-09-12 09:10:30",
    updated_at: "2026-09-12 09:10:30",
    ...over,
  });

  it("fills the fields it holds, and leaves the password alone", async () => {
    // A profile carries no password by design. Applying one must therefore not
    // clear a password that has just been typed, and must never appear to
    // supply one that was never stored.
    getFleetProfiles.mockResolvedValue([profile()]);
    await show([collector({ has_password: false, ready: false })]);
    const password = screen.getByLabelText(
      "SSH password for edge1",
    ) as HTMLInputElement;
    fireEvent.change(password, { target: { value: "typed-by-hand" } });

    fireEvent.change(screen.getByLabelText("Source for edge1"), {
      target: { value: "7" },
    });

    expect((screen.getByLabelText("Address for edge1") as HTMLInputElement).value).toBe(
      "192.168.68.63",
    );
    expect((screen.getByLabelText("SSH port for edge1") as HTMLInputElement).value).toBe(
      "2222",
    );
    expect((screen.getByLabelText("SSH user for edge1") as HTMLInputElement).value).toBe(
      "gavin",
    );
    expect((screen.getByLabelText("SSH password for edge1") as HTMLInputElement).value).toBe(
      "typed-by-hand",
    );
  });

  it("is saved from a card without the password in it", async () => {
    // The one way a password could reach disk: the Save button copying the
    // card's fields wholesale. It sends five, and none of them is that one.
    createFleetProfile.mockResolvedValue(profile());
    await show([collector({ has_password: false, ready: false })]);
    fireEvent.change(screen.getByLabelText("SSH password for edge1"), {
      target: { value: "hunter2" },
    });
    screen.getByLabelText("Save edge1 as a profile").click();

    fireEvent.change(await screen.findByLabelText("Profile name"), {
      target: { value: "lab-edge" },
    });
    button("Save profile")!.click();

    await waitFor(() => expect(createFleetProfile).toHaveBeenCalled());
    const sent = createFleetProfile.mock.calls[0][0];
    expect(sent).toEqual({
      name: "lab-edge",
      device_name: "Edge collector (lab)",
      host: "10.0.0.9",
      port: 22,
      username: "dut",
      scope: "private",
    });
    expect(JSON.stringify(sent)).not.toContain("hunter2");
  });

  it("sends the reader to the page that owns them rather than editing here", async () => {
    await show([collector()]);
    screen.getByLabelText("Manage saved profiles").click();
    expect(manageProfiles).toHaveBeenCalled();
  });
});

describe("who sees this at all", () => {
  it("draws nothing for an engineer", async () => {
    // Every /api/collectors route is admin, so a form here could only 403.
    role = "engineer";
    const { container } = render(<FleetHostsSection onManageProfiles={manageProfiles} />);
    expect(container.firstChild).toBeNull();
    expect(getCollectors).not.toHaveBeenCalled();
  });
});

describe("the host key, where the operator already is", () => {
  /**
   * An unknown host key is reported and never accepted for the operator, and
   * the way out used to be a terminal trip. This panel makes the same decision
   * available where the work is — and keeps it a decision: what is trusted is
   * the fingerprint on screen, not whatever answers next.
   */
  function keyStatus(over: Partial<import("../api/rest").HostKeyStatus> = {}) {
    return {
      host: "10.0.0.9",
      port: 22,
      known: false,
      known_keys: [],
      presented_keys: [{ type: "ssh-ed25519", bits: "256", fingerprint: "SHA256:LIVEkey" }],
      matches: null,
      scan_error: null,
      ...over,
    };
  }

  async function openDetails(status: ReturnType<typeof keyStatus>) {
    getCollectorHostKey.mockResolvedValue(status);
    await show([collector()]);
    button("Details")!.click();
    await screen.findByText("Host key");
  }

  it("offers to trust a key this machine has never seen, and says what that is worth", async () => {
    await openDetails(keyStatus());
    expect(screen.getByText("not known to this machine")).toBeTruthy();
    expect(screen.getByText("SHA256:LIVEkey")).toBeTruthy();
    expect(button("Trust this key")).toBeTruthy();
    // Said plainly rather than implied: this is trust on first use, and the
    // only real check is reading the fingerprint off the box itself.
    expect(screen.getByText(/same trust-on-first-use/)).toBeTruthy();
    expect(screen.getByText(/ssh_host_ed25519_key.pub/)).toBeTruthy();
  });

  it("trusts the fingerprint that was shown, not the host in general", async () => {
    // The whole difference from StrictHostKeyChecking=accept-new: the answer
    // names a key, and the backend re-reads before it writes.
    trustCollectorHostKey.mockResolvedValue({ ok: true, trusted: "SHA256:LIVEkey", type: "ssh-ed25519" });
    await openDetails(keyStatus());
    button("Trust this key")!.click();
    await waitFor(() =>
      expect(trustCollectorHostKey).toHaveBeenCalledWith("edge1", "SHA256:LIVEkey"),
    );
  });

  it("offers nothing to press when a key is already on record", async () => {
    await openDetails(keyStatus({
      known: true,
      known_keys: [{ type: "ssh-ed25519", bits: "256", fingerprint: "SHA256:LIVEkey" }],
      matches: true,
    }));
    expect(screen.getByText("on record, and it matches")).toBeTruthy();
    expect(button("Trust this key")).toBeUndefined();
  });

  it("says stop when the host presents something else", async () => {
    await openDetails(keyStatus({
      known: true,
      known_keys: [{ type: "ssh-ed25519", bits: "256", fingerprint: "SHA256:OLDkey" }],
      matches: false,
    }));
    expect(screen.getByText(/presenting a different one/)).toBeTruthy();
    // Nothing on this page overwrites it: a reimaged box and a replaced one
    // look the same from here.
    expect(button("Trust this key")).toBeUndefined();
    expect(screen.getByText(/ssh-keygen -R/)).toBeTruthy();
  });

  it("does not read an unreachable box as a changed key", async () => {
    await openDetails(keyStatus({
      known: true,
      known_keys: [{ type: "ssh-ed25519", bits: "256", fingerprint: "SHA256:LIVEkey" }],
      presented_keys: [],
      matches: null,
      scan_error: "No SSH key came back from 10.0.0.9:22 — ssh-keyscan said: No route to host",
    }));
    expect(screen.getByText(/nothing answered just now/)).toBeTruthy();
    expect(screen.queryByText(/presenting a different one/)).toBeNull();
    expect(screen.getByText(/No route to host/)).toBeTruthy();
  });
});
