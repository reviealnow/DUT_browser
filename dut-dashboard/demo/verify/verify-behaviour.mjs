// What the controls on these pages actually do.
//
// Every assertion here was a review finding first, and each is the kind that
// reading the source did not catch: a control that existed but did the wrong
// thing, or one the product has that the demo quietly lacked. The failure runs
// both ways — the demo must not accept what the product refuses, and must not
// show something cruder either — so this file checks outcomes, not markup.
import { load, click, reporter, pageErrors } from "./harness.mjs";

const report = reporter();

// ---------------------------------------------------------------- downloads
{
  report.section("downloads.html — one expand control, paired context, embedded plots");
  const window = await load("downloads.html");
  const document = window.document;
  const cells = document.querySelector("#logs tr").querySelectorAll("td");

  // DownloadsSection has ONE twist in the name cell that opens the session's
  // context and the log tail together. Two controls read as two capabilities.
  report.ok("no second 'Show context' control", !document.querySelector("[data-context]"));
  report.ok("the expand control is in the name cell",
    cells[0].querySelectorAll("button").length === 1 &&
    cells[0].querySelector("button").hasAttribute("data-peek"));
  report.ok("the actions cell holds only Analyze and Download",
    [...cells[3].querySelectorAll("button")].map(b => b.textContent.trim()).join(",") === "Analyze,⬇");

  click(window, document.querySelector("[data-peek]"));
  const expanded = document.querySelector("#logs tr.expandrow td");
  report.ok("one expand row, spanning the table",
    document.querySelectorAll("#logs tr.expandrow").length === 1 &&
    expanded.getAttribute("colspan") === "4");
  report.ok("it opens the context AND the log tail together",
    !!expanded.querySelector(".session-context") && !!expanded.querySelector("pre.peek"));

  // SessionContext pairs captures on the stem, so two captures of one kind stay
  // two entries; grouping by kind merged them.
  const stems = [...expanded.querySelectorAll(".session-context-list li")]
    .map(li => li.querySelector(".mono").textContent);
  report.ok("context is paired by stem, not grouped by kind",
    stems.length > 0 && new Set(stems).size === stems.length, stems.join(" | "));

  // The product keeps open previews in a Set.
  const plots = [...document.querySelectorAll("#outputs [data-plot]")];
  click(window, plots[0]);
  click(window, [...document.querySelectorAll("#outputs [data-plot]")][1]);
  report.ok("several plot previews open at once",
    document.querySelectorAll("#outputs tr.expandrow").length === 2);

  // Every plot the bundle produced travels in the file. Two are tables rendered
  // to pixels and are redrawn with aliases — they must say so.
  let embedded = 0, redrawn = 0;
  const missing = [];
  for (const name of plots.map(b => b.dataset.plot)) {
    const button = [...document.querySelectorAll("#outputs [data-plot]")]
      .find(b => b.dataset.plot === name);
    if (button.textContent.trim() === "▸") click(window, button);
    const cell = [...document.querySelectorAll("#outputs tr.expandrow td")]
      .find(td => td.querySelector(`img[alt="${name}"]`));
    const image = cell && cell.querySelector("img.plot-preview");
    if (image && image.getAttribute("src").startsWith("data:image/png;base64,")) {
      embedded++;
      if (cell.querySelector(".redrawn")) redrawn++;
    } else missing.push(name);
  }
  report.ok("every plot is embedded as a real image", missing.length === 0,
    `missing: ${missing.join(", ")}`);
  report.ok("the redrawn tables disclose that they are redrawn", redrawn === 2,
    `${redrawn} disclosed`);
  report.ok("nothing still claims the plots were left out to keep the page small",
    !document.body.textContent.includes("stop the page"));

  report.ok("downloads.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

// ----------------------------------------------------------- serial console
{
  report.section("serial-console.html — Send never writes to the Monitor");
  const window = await load("serial-console.html");
  const document = window.document;
  const before = document.getElementById("console").textContent;

  // Offline there is no DUT to echo the line back, and the Monitor's provenance
  // line calls its contents real console output. Composing a prompt here would
  // put invented text inside a view claimed as measured.
  document.getElementById("command").value = "uname -a";
  document.getElementById("sendForm")
    .dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  report.ok("the console is unchanged after Send",
    document.getElementById("console").textContent === before);
  report.ok("no synthetic prompt line was added",
    !document.getElementById("console").textContent.includes("uname -a"));
  report.ok("the toast says where the line went",
    document.getElementById("toast").textContent.includes("no DUT"));

  document.getElementById("popupText").value = "ls /mnt/data\ncat /proc/uptime";
  click(window, document.getElementById("popupSend"));
  report.ok("the popup editor's Send is equally inert",
    document.getElementById("console").textContent === before);
  report.ok("the offline limit is stated on the send row",
    !!document.querySelector(".sendrow .concept"));

  report.ok("serial-console.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

// ----------------------------------------------------------------- firmware
{
  report.section("firmware.html — the transport rule, in the service's own order");
  const window = await load("firmware.html");
  const document = window.document;
  const fire = (id, event) => document.getElementById(id)
    .dispatchEvent(new window.Event(event, { bubbles: true }));
  // The upload path is a radio group, not a <select>: click the option the way
  // an operator does rather than assigning a value the control no longer has.
  const setTransport = (value) => {
    const radio = document.querySelector(`#transport input[value="${value}"]`);
    radio.checked = true;
    radio.dispatchEvent(new window.Event("change", { bubbles: true }));
  };
  const setImage = (prefix) => {
    const option = [...document.getElementById("image").options].find(o => o.textContent.startsWith(prefix));
    document.getElementById("image").value = option.value;
    fire("image", "change");
  };
  const setAddress = (value) => { document.getElementById("mgmt").value = value; fire("mgmt", "input"); };
  const setExpected = (value) => { document.getElementById("expected").value = value; fire("expected", "input"); };
  // What the operator is told and whether the one action is available. This
  // replaces the old rehearse-button probe: the product has no such button, so
  // a demo that kept one would be showing a control that does not exist.
  const summary = () => document.getElementById("actionSummary").textContent;
  const canFlash = () => !document.getElementById("btnUpgrade").disabled;

  report.ok("the capability boundary is stated next to the actions",
    document.querySelector(".bounds")?.textContent.includes("does not upload") === true);

  report.ok("both upload paths are readable at once, not hidden behind a dropdown",
    document.querySelectorAll("#transport .fw-choice").length === 2 &&
    document.querySelector("#transport").textContent.includes("signed") &&
    document.querySelector("#transport").textContent.includes("encrypted"));

  report.ok("the workspace picker groups images apart from everything else",
    [...document.querySelectorAll("#image optgroup")].map(g => g.label).join("|")
      === "Firmware images|Other workspace files (not recognised as firmware)");

  report.ok("nothing can be flashed before an image is chosen",
    !canFlash() && summary().includes("Choose a firmware image"));

  // normalise_mgmt_url: an explicit port is always kept; a missing one gets the
  // transport's default.
  setAddress("https://198.51.100.20:9443");
  setTransport("api");
  setImage("ubi_kernel_DemoDUT-6E-encrypt_");
  report.ok("an explicit port is kept, not appended to",
    document.getElementById("addrNote").textContent.includes("198.51.100.20:9443/ap/") &&
    !document.getElementById("addrNote").textContent.includes(":9443:10443"));
  setAddress("198.51.100.20");
  report.ok("a bare address gets the transport's own port",
    document.getElementById("addrNote").textContent.includes("https://198.51.100.20:10443/ap/"));

  report.ok("a correct pairing names the exact target on the button's own line",
    canFlash() && summary().includes("198.51.100.20:10443/ap/systemctl/sysFwUpgrade"));

  // check_image_for_transport: the API takes the encrypted image, the web UI the
  // signed one — and an unrecognised name is allowed through on purpose.
  setImage("wifix.tar.gz.sig");
  report.ok("API + signed .sig is refused on screen",
    document.getElementById("mismatch").textContent.includes("only the web UI accepts"));
  report.ok("…and the refusal reaches the action, without repeating itself there",
    !canFlash() && summary().includes("does not fit the selected upload path") &&
    !summary().includes("only the web UI accepts"));

  setTransport("gui");
  setImage("release-notes-1.10.339.txt");
  report.ok("an unrecognised filename is allowed, as the backend allows it",
    document.getElementById("mismatch").textContent === "" && canFlash());

  /* The service checks pairing, then checksum, then address. The checksum is the
     one gate that must NOT disable the button: the product posts and lets
     firmware_service refuse, so a demo that blocked here would teach a stricter
     rule than ships. */
  setImage("wifix.tar.gz.sig");
  setExpected("deadbeef");
  report.ok("a wrong expected checksum is shown but does not pre-empt the service",
    document.getElementById("mismatch").textContent.includes("does not match") && canFlash());
  setExpected("");
  setAddress("");
  report.ok("no management address blocks the upgrade, as the service does",
    !canFlash() && summary().includes("No management address"));

  report.ok("firmware.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

// --------------------------------------------------------- ssid capability
{
  report.section("ssid-capability.html — the product's eleven columns");
  const window = await load("ssid-capability.html");
  const document = window.document;
  const first = document.querySelector("#tbody tr");

  // CapabilityRowView renders eleven cells and puts the expand control inside
  // the Iface cell; a column of its own made the table twelve wide.
  report.ok("the header has eleven columns",
    document.querySelectorAll("thead th").length === 11);
  report.ok("a body row has eleven cells", first.querySelectorAll("td").length === 11);
  report.ok("the expand control is inside the Iface cell",
    !!first.querySelectorAll("td")[0].querySelector("button.twist"));
  click(window, document.querySelector("#tbody button.twist"));
  report.ok("the expanded row spans eleven",
    document.querySelector("tr.cap-expand td").getAttribute("colspan") === "11");

  report.ok("ssid-capability.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

// ------------------------------------------------------------ fleet strip
{
  report.section("overview.html — the fleet strip's remote-node card");
  const window = await load("overview.html");
  const document = window.document;
  const cards = [...document.querySelectorAll(".fleet-card")];
  const rowsOf = (card) => Object.fromEntries([...card.querySelectorAll(".stat-row")]
    .map(r => [r.querySelector("dt").textContent.trim(), r.querySelector("dd").textContent.trim()]));

  const mother = cards.find(c => c.querySelector(".fleet-where").textContent.includes("Mother"));
  const node = cards.find(c => c.querySelector(".fleet-where").textContent.includes("Remote via")
    && rowsOf(c)["Uplink to parent"]?.includes("dBm"));
  const root = cards.find(c => rowsOf(c)["Uplink to parent"] === "None — this is the root");

  // FleetStrip renders the two SSH rows only when the DUT has an SSH console.
  // A strip of mother-server cards alone would tell a viewer the product
  // cannot reach a DUT over a Pi at all.
  report.ok("a mother-server card carries no SSH rows",
    !!mother && !("SSH session" in rowsOf(mother)) && !("Node console" in rowsOf(mother)));
  report.ok("a remote node shows all four remote rows",
    !!node && ["SSH session", "Node console", "Uplink to parent", "Children on backhaul"]
      .every(k => k in rowsOf(node)));
  // The backhaul rows are not remote-node rows. Both commands behind them run
  // over a cabled console as well as an SSH one, and the fleet's root is
  // frequently the DUT on this desk — the card that used to be the one place
  // the measurement could never appear.
  report.ok("a mother-server card still reports both backhaul directions",
    !!mother && ["Uplink to parent", "Children on backhaul"].every(k => k in rowsOf(mother)));

  // Up and down are separate measurements from separate commands; a root has
  // no parent and says so rather than reading as an uncaptured value.
  report.ok("the root's uplink is an answer, not a gap", !!root);
  report.ok("the root still reports children",
    !!root && rowsOf(root)["Children on backhaul"].includes("dBm"));
  report.ok("a cabled card offers Refresh RSSI too",
    !!node?.querySelector("[data-act=rssi]") && !!mother?.querySelector("[data-act=rssi]"));
  report.ok("and it is offered live, not disabled, on an open cabled console",
    !mother?.querySelector("[data-act=rssi]").disabled);

  // The strip renders FleetCard, which gates each control on the role its own
  // route needs: Console is engineer, Connect and Close serial are admin on a
  // REMOTE node, and Refresh RSSI is admin on EVERY card because the capture is
  // /api/fleet whichever console it runs on. So the role this page portrays and
  // the buttons it draws have to agree — it used to say "engineer" over a strip
  // full of admin-only controls, which is the demo showing what the product
  // hides.
  //
  // `adminOnly` lists the controls that actually NEED admin, rather than the
  // ones that merely drive the DUT, and it reads the cabled card as well as the
  // remote one. Both halves were once weaker than they looked: the earlier form
  // was `.some(["connect","close","rssi"])` over `node` alone, so it neither
  // noticed a cabled card at all nor could have failed on one — a cabled card
  // that had lost Refresh RSSI still passed on its Close serial, which an
  // engineer may press. Once the cabled DUT's backhaul became capturable,
  // remoteness stopped being what forces this badge.
  const adminOnly = (card) => [
    // The capture is /api/fleet on every card, cabled or remote.
    ...(card.querySelector("[data-act=rssi]") ? ["rssi"] : []),
    // Connect / Close serial are engineer on a cabled DUT; only a remote node's
    // pair are admin, because they open and close an SSH session.
    ...(card.querySelector(".fleet-where").textContent.includes("Remote via")
      ? ["connect", "close"].filter((act) => card.querySelector(`[data-act=${act}]`))
      : []),
  ];
  report.ok("the role the page portrays covers every control it draws",
    document.querySelector(".pill-admin")?.textContent.trim() === "admin" &&
    adminOnly(node).length > 0 && adminOnly(mother).length > 0,
    document.querySelector(".topbar")?.textContent.trim());

  // Refresh RSSI re-reads the backhaul and touches nothing else. Sharing the
  // connect/close path would have made it a connection control wearing another
  // label — and on an already-open card that is invisible in the card's own
  // state, so the assertion is on what the page says it did.
  click(window, node.querySelector("[data-act=rssi]"));
  await new Promise(r => setTimeout(r, 900));
  const said = document.getElementById("toast").textContent;
  report.ok("Refresh RSSI reports a re-read, not a connection change",
    said.includes("backhaul re-read"), said);
  const after = [...document.querySelectorAll(".fleet-card")]
    .find(c => rowsOf(c)["Uplink to parent"]?.includes("dBm"));
  report.ok("and the reading survives the re-read",
    !!after && rowsOf(after)["Uplink to parent"].includes("dBm"));

  // Everything below reads `live`, not `cards`: the refresh above re-rendered
  // the strip, so every node captured before it is detached. Assertions against
  // detached nodes pass while the page on screen is wrong — two of these did.
  const live = [...document.querySelectorAll(".fleet-card")];

  // A standalone AP has no mesh backhaul, so the product answers "the question
  // does not apply" in both directions and refuses the control — a different
  // statement from "we have not measured yet", and the demo showed neither.
  const standalone = live.find(c => rowsOf(c)["Uplink to parent"] === "Not applicable");
  report.ok("a standalone remote reads Not applicable both ways",
    !!standalone && rowsOf(standalone)["Children on backhaul"] === "Not applicable");
  const refusal = standalone?.querySelector("[data-act=rssi]");
  report.ok("and its Refresh RSSI is refused for being standalone, not for being closed",
    !!refusal?.disabled && /standalone/i.test(refusal.title), refusal?.title);

  // The strip is the product's DUT switcher: the card body selects. Only the
  // buttons acted here, so the card read as a display, not a control. The pill
  // alone cannot tell selection from the Console action — both move it — so
  // this asserts which branch ran.
  const other = live.find(c => c.querySelector(".fleet-name").textContent !== "DemoDUT-6E");
  click(window, other.querySelector(".fleet-name"));
  const label = other.querySelector(".fleet-name").textContent;
  report.ok("clicking a card body selects that DUT",
    document.getElementById("dutPill").textContent === label &&
    document.getElementById("toast").textContent === `Selected ${label}`,
    document.getElementById("toast").textContent);

  // Named, not counted: `size >= 3` passed with any three strings at all. These
  // are STATUS_META's labels — the product never prints a state name, and this
  // page used to print "idle".
  const statuses = new Set(live.map(c => c.querySelector(".pill").textContent.trim()));
  report.ok("all three of FleetStrip's status labels are reachable",
    ["Streaming", "No DUT", "Offline"].every(s => statuses.has(s)), [...statuses].join(","));
  report.ok("a disconnected card still offers Connect",
    live.some(c => c.querySelector("[data-act=connect]")));

  // A mesh remote nobody has connected: both directions unmeasured, and the
  // control refused for the other reason. "Not captured" and "Not applicable"
  // are different answers and the strip has to be able to say both.
  const unconnected = live.find(c => rowsOf(c)["Uplink to parent"] === "Not captured");
  const closedRefusal = unconnected?.querySelector("[data-act=rssi]");
  report.ok("an unconnected mesh remote reads Not captured, not Not applicable",
    !!unconnected && rowsOf(unconnected)["Children on backhaul"] === "Not captured");
  report.ok("its Refresh RSSI is refused for the console, not for being standalone",
    !!closedRefusal?.disabled && /console/i.test(closedRefusal.title), closedRefusal?.title);

  // Connecting a remote node runs the capture, as FleetStrip's onConnect does.
  // A demo where the rows only fill on Refresh would understate the control.
  click(window, unconnected.querySelector("[data-act=connect]"));
  await new Promise(r => setTimeout(r, 900));
  const connected = [...document.querySelectorAll(".fleet-card")]
    .find(c => c.querySelector(".fleet-name").textContent === "DemoNode-lab3");
  report.ok("connecting a remote node captures its backhaul without a second press",
    rowsOf(connected)["Uplink to parent"].includes("dBm"),
    rowsOf(connected)["Uplink to parent"]);

  // Close asks first, and a refusal leaves the session alone — the product puts
  // a confirm in front of an outward state change. The stub is a spy, because
  // observing only the end state cannot tell a working gate from an
  // implementation that never asks: one that cancels the first Close and closes
  // on the second passes both outcome checks while asking nobody anything.
  const asked = [];
  window.confirm = (message) => { asked.push(message); return false; };
  click(window, connected.querySelector("[data-act=close]"));
  await new Promise(r => setTimeout(r, 900));
  const afterCancel = [...document.querySelectorAll(".fleet-card")]
    .find(c => c.querySelector(".fleet-name").textContent === "DemoNode-lab3");
  report.ok("Close asks before it closes, naming the DUT",
    asked.length === 1 && asked[0].includes("DemoNode-lab3"), asked.join(" | "));
  report.ok("declining the confirm leaves the session open",
    !!afterCancel.querySelector("[data-act=close]"),
    document.getElementById("toast").textContent);

  window.confirm = (message) => { asked.push(message); return true; };
  click(window, afterCancel.querySelector("[data-act=close]"));
  await new Promise(r => setTimeout(r, 900));
  const afterClose = [...document.querySelectorAll(".fleet-card")]
    .find(c => c.querySelector(".fleet-name").textContent === "DemoNode-lab3");
  report.ok("it asks again on the second attempt", asked.length === 2, String(asked.length));
  report.ok("accepting it closes the session",
    !!afterClose.querySelector("[data-act=connect]"));

  report.ok("overview.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

// ----------------------------------------------------------- fleet section
{
  report.section("fleet.html — the capture the Overview strip has no room for");
  const window = await load("fleet.html");
  const document = window.document;
  const byName = (name) => [...document.querySelectorAll(".fleet-card")]
    .find(c => c.querySelector(".fleet-name").textContent === name);
  const pairs = (nodes) => Object.fromEntries([...nodes]
    .map(r => [r.querySelector("dt").textContent.trim(), r.querySelector("dd").textContent.trim()]));
  // The card's own rows and the unfolded capture's rows are both `.stat-row`,
  // and they answer different questions — a lookup that merged them would read
  // the detail's "Interface" as something the card claims.
  const cardRows = (card) => pairs(card.querySelectorAll(":scope > dl.stat-list .stat-row"));
  const detailRows = (card) => pairs(card.querySelectorAll(".fleet-detail .stat-row"));

  // The three fields that had no reader anywhere in the frontend until this
  // section existed. Asserted by value: a header row spelled right over an
  // empty cell is the failure this page is for.
  const node = byName("DemoNode-420");
  const link = detailRows(node);
  report.ok("a captured uplink shows SNR, radio band and the parent's BSSID",
    link["SNR"] === "49 dB" && link["Band"] === "5GHz" &&
    /^02:[0-9a-f:]{14}$/.test(link["Parent BSSID"]), JSON.stringify(link));
  report.ok("…and the strip's two compressed lines are still on the card",
    cardRows(node)["Uplink to parent"] === "-37 dBm · near" &&
    cardRows(node)["Children on backhaul"] === "None", JSON.stringify(cardRows(node)));
  report.ok("a backhaul with nobody on it says so rather than showing an empty table",
    node.textContent.includes("No children associated.") &&
    !node.querySelector(".fleet-peer-table"));

  // Per-child, which is the measurement that says WHICH child is hearing badly.
  // The strip joins every child into one string and cannot.
  const root = byName("DemoRoot-840E");
  const peers = [...root.querySelectorAll(".fleet-peer-table tbody tr")];
  report.ok("every child is its own row, with its own signal and quality",
    peers.length === 3 &&
    new Set(peers.map(r => r.querySelectorAll("td")[1].textContent)).size === 3 &&
    peers.every(r => /^02:/.test(r.querySelector("td").textContent)),
    peers.map(r => r.textContent.trim()).join(" | "));
  report.ok("a root's uplink block is an answer, not a gap",
    root.querySelector(".fleet-detail").textContent.includes("None — this is the root of the mesh.")
    && !("SNR" in detailRows(root)));
  // The bench case this wording exists for: a configured VAP carrying an
  // ordinary laptop. "detected" and "configured" are different claims.
  report.ok("an unverified backhaul interface is disclosed as configured",
    root.querySelector(".fleet-detail").textContent
      .includes("configured — nobody verified this interface is a backhaul"));
  report.ok("a detected one is not dressed up as the same thing",
    node.querySelector(".fleet-detail").textContent.includes("detected backhaul"));

  // Four states that must not read alike: never captured, does not apply, a
  // measured root, and a measured DUT with no parent that is not known root.
  const unconnected = byName("DemoNode-lab3");
  report.ok("a mesh node nobody captured says so instead of showing zeros",
    !!unconnected.querySelector(".fleet-detail-empty") &&
    unconnected.textContent.includes("No capture yet"));
  const standalone = byName("DemoAP-standalone");
  report.ok("a standalone remote has no capture to unfold, and says Not applicable",
    !standalone.querySelector(".fleet-detail") &&
    cardRows(standalone)["Uplink to parent"] === "Not applicable" &&
    cardRows(standalone)["Children on backhaul"] === "Not applicable");
  const mother = byName("DemoDUT-6E");
  report.ok("a mother-server card carries no SSH rows but does unfold its backhaul",
    !("SSH session" in cardRows(mother)) &&
    detailRows(mother)["Parent BSSID"] === "02:1f:6c:44:9a:31");
  const noParent = byName("DemoDUT-bench3");
  report.ok("a captured cabled DUT with no parent is not called a root",
    cardRows(noParent)["Uplink to parent"] === "None — no parent found" &&
    cardRows(noParent)["Children on backhaul"] === "No backhaul VAP identified" &&
    noParent.querySelector(".fleet-detail-empty").textContent.includes("Either it is standalone"));

  // FleetSection's one section-level action. The count is the set it captures,
  // and the order is load-bearing: a root cannot name its own backhaul VAP, so
  // capturing it before its children falls back to a configured guess.
  report.ok("Capture all counts only the mesh nodes with a console open",
    document.getElementById("btnCaptureAll").textContent.trim() === "Capture all (3)",
    document.getElementById("btnCaptureAll").textContent);
  report.ok("…and says it reads nodes before roots",
    /nodes first, then roots/.test(document.getElementById("btnCaptureAll").title));
  report.ok("the section states that nothing here refreshes on its own",
    document.querySelector(".fleet-section-toolbar .setting-hint").textContent
      .includes("nothing here refreshes on its own"));

  // Every control is gated on the role its own route needs, and this page is
  // shown as the one role that sees a remote card whole. A viewer who is not
  // told that would read the card as what everyone gets.
  report.ok("the page says which role it is showing",
    document.querySelector(".pill-admin").textContent.trim() === "admin");
  report.ok("…and states the per-route rule behind the buttons",
    /every .*\/api\/fleet.* route is admin/s.test(
      document.getElementById("provenance").textContent) &&
    document.getElementById("provenance").textContent.includes("none of the buttons"));

  // Refusals: two different reasons, and the demo has to be able to say both.
  report.ok("a standalone's Refresh RSSI is refused for being standalone",
    /standalone/i.test(standalone.querySelector("[data-act=rssi]").title) &&
    standalone.querySelector("[data-act=rssi]").disabled);
  report.ok("a closed console's is refused for the console",
    /console/i.test(unconnected.querySelector("[data-act=rssi]").title) &&
    unconnected.querySelector("[data-act=rssi]").disabled);
  report.ok("a cabled card offers Refresh RSSI too",
    !!mother.querySelector("[data-act=rssi]") && !mother.querySelector("[data-act=rssi]").disabled);
  report.ok("a closed cabled card refuses Refresh RSSI for the console",
    !!noParent.querySelector("[data-act=rssi]")?.disabled &&
    /console/i.test(noParent.querySelector("[data-act=rssi]")?.title || ""));

  // The detail sits OUTSIDE the button that selects the DUT, so reading a
  // peer's RSSI must not switch the DUT under whoever is reading it.
  const pill = () => document.getElementById("dutPill").textContent;
  const before = pill();
  click(window, root.querySelector(".fleet-peer-table td"));
  report.ok("clicking inside the unfolded capture does not select that DUT",
    pill() === before, pill());
  click(window, root.querySelector(".fleet-name"));
  report.ok("clicking the card body does",
    pill() === "DemoRoot-840E" &&
    document.getElementById("toast").textContent === "Selected DemoRoot-840E");

  // The order Capture all actually reads in, not the order its title promises.
  // A child reports the uplink that names its root's backhaul VAP, so the root
  // is asked second — and a root nobody read blind is not sent round twice.
  // Everything below re-queries, because this re-renders the grid: assertions
  // against nodes captured before it would pass over a detached tree.
  click(window, document.getElementById("btnCaptureAll"));
  await new Promise(r => setTimeout(r, 2600));
  const said = document.getElementById("toast").textContent;
  report.ok("Capture all reads the child before the root, each exactly once",
    said.includes("DemoDUT-6E → DemoNode-420 → DemoRoot-840E") &&
    said.includes("3 backhauls"), said);

  // Connecting a remote node runs the capture, as FleetCard's onConnect does.
  click(window, byName("DemoNode-lab3").querySelector("[data-act=connect]"));
  await new Promise(r => setTimeout(r, 900));
  const joined = byName("DemoNode-lab3");
  report.ok("connecting a mesh node unfolds its capture without a second press",
    detailRows(joined)["SNR"] === "33 dB" &&
    detailRows(joined)["Parent BSSID"] === "02:1f:6c:44:9a:31",
    JSON.stringify(detailRows(joined)));
  report.ok("and the section's own count follows the console it just opened",
    document.getElementById("btnCaptureAll").textContent.trim() === "Capture all (4)",
    document.getElementById("btnCaptureAll").textContent);

  // Close asks first, and the stub is a spy: observing only the end state
  // cannot tell a working gate from one that never asks.
  const asked = [];
  window.confirm = (message) => { asked.push(message); return false; };
  click(window, byName("DemoNode-lab3").querySelector("[data-act=close]"));
  await new Promise(r => setTimeout(r, 900));
  report.ok("Close asks before it closes, naming the DUT",
    asked.length === 1 && asked[0].includes("DemoNode-lab3"), asked.join(" | "));
  report.ok("declining leaves the session open",
    !!byName("DemoNode-lab3").querySelector("[data-act=close]"));

  // -- mesh topology --------------------------------------------------------
  // The block exists because the cards and the DUT's own console disagreed: the
  // device listed members the fleet had no card for. A demo that only listed
  // the members it already has cards for would reproduce exactly the defect,
  // and look complete doing it.
  const meshRows = [...document.querySelectorAll("[data-mesh-row]")];
  const cells = (row) => [...row.querySelectorAll("td")].map(td => td.textContent.trim());
  report.ok("the mesh table lists every member the device reports, cards or not",
    meshRows.length === 2, String(meshRows.length));

  const unregistered = meshRows.filter(r => cells(r)[6] === "Not registered here");
  report.ok("members with no DUT here are marked, not quietly listed",
    unregistered.length === 1, unregistered.map(r => cells(r)[3]).join(","));
  report.ok("a member that does have one names it and its console state",
    meshRows.some(r => /DemoRoot-840E · console open/.test(cells(r)[6])),
    meshRows.map(r => cells(r)[6]).join(" | "));

  // The device sends `signal: 0` for a root. Printed as a number it reads as
  // the strongest link on the bench; printed as "—" it reads as unmeasured.
  const rootRow = meshRows.find(r => cells(r)[1] === "Root");
  report.ok("a root's absent signal is neither a number nor a bare dash",
    cells(rootRow)[5] === "n/a — root", cells(rootRow)[5]);
  report.ok("a node's measured signal keeps its number and band",
    meshRows.some(r => cells(r)[5] === "-26 dBm · near"),
    meshRows.map(r => cells(r)[5]).join(" | "));

  // Two of six DUTs carry a management address, so the picker has a choice to
  // offer. It is hidden where there is none, as in the product.
  report.ok("the source picker lists only DUTs that can be asked",
    document.querySelectorAll("#meshSource option").length === 2);

  click(window, document.getElementById("btnMeshRefresh"));
  report.ok("Refresh mesh re-reads and says so",
    [...document.querySelectorAll("[data-mesh-row]")].length === 2 &&
    /mesh/i.test(document.getElementById("toast").textContent),
    document.getElementById("toast").textContent);

  // -- what the device says about its own mesh ------------------------------
  // Four claims, and a demo that shows fewer than four is a demo where the
  // distinction cannot be seen. The one that matters: "could not tell" must not
  // render as "no mesh" -- that prints a confident wrong answer over a device
  // that is meshed and healthy.
  const probeRows = [...document.querySelectorAll(".fleet-card")]
    .map(c => pairs(c.querySelectorAll(".stat-row"))["Mesh (device says)"])
    .filter(Boolean);
  report.ok("every card carries the device's own mesh answer",
    probeRows.length === 6, String(probeRows.length));
  for (const [label, wanted] of [
    ["reported members", "2 members reported"],
    ["a device that says it has none", "No mesh on this device"],
    ["one nobody has asked", "Not probed"],
    ["one we could not read", "Could not tell"],
  ]) {
    report.ok(`the row can say: ${label}`, probeRows.includes(wanted), probeRows.join(" | "));
  }

  report.ok("fleet.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

report.finish();
