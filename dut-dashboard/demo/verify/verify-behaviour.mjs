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
  report.ok("…and the refusal reaches the action, not just a paragraph above it",
    !canFlash() && summary().includes("only the web UI accepts"));

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

report.finish();
