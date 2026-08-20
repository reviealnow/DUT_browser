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

  // FleetStrip renders the four remote rows only when the DUT has an SSH
  // console. A strip of mother-server cards alone would tell a viewer the
  // product cannot reach a DUT over a Pi at all.
  report.ok("a mother-server card carries no remote rows",
    !!mother && !("SSH session" in rowsOf(mother)));
  report.ok("a remote node shows all four remote rows",
    !!node && ["SSH session", "Node console", "Uplink to parent", "Children on backhaul"]
      .every(k => k in rowsOf(node)));

  // Up and down are separate measurements from separate commands; a root has
  // no parent and says so rather than reading as an uncaptured value.
  report.ok("the root's uplink is an answer, not a gap", !!root);
  report.ok("the root still reports children",
    !!root && rowsOf(root)["Children on backhaul"].includes("dBm"));
  report.ok("only remote cards offer Refresh RSSI",
    !!node?.querySelector("[data-act=rssi]") && !mother?.querySelector("[data-act=rssi]"));

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

  // Named, not counted: `size >= 3` passed with any three strings at all, and
  // passed while the statuses were not ones the product renders.
  const statuses = new Set(live.map(c => c.querySelector(".pill").textContent.trim()));
  report.ok("the statuses are the ones FleetStrip renders",
    ["streaming", "idle", "no DUT"].every(s => statuses.has(s)), [...statuses].join(","));
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

  // Close asks first, and a refusal leaves the session alone — the product
  // puts a confirm in front of an outward state change.
  window.confirm = () => false;
  click(window, connected.querySelector("[data-act=close]"));
  await new Promise(r => setTimeout(r, 900));
  const afterCancel = [...document.querySelectorAll(".fleet-card")]
    .find(c => c.querySelector(".fleet-name").textContent === "DemoNode-lab3");
  report.ok("declining the confirm leaves the session open",
    !!afterCancel.querySelector("[data-act=close]"),
    document.getElementById("toast").textContent);

  window.confirm = () => true;
  click(window, afterCancel.querySelector("[data-act=close]"));
  await new Promise(r => setTimeout(r, 900));
  const afterClose = [...document.querySelectorAll(".fleet-card")]
    .find(c => c.querySelector(".fleet-name").textContent === "DemoNode-lab3");
  report.ok("accepting it closes the session",
    !!afterClose.querySelector("[data-act=connect]"));

  report.ok("overview.html threw nothing while all that ran",
    pageErrors(window).length === 0, pageErrors(window).join(" | "));
}

report.finish();
