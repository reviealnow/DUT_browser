#!/usr/bin/env node
/**
 * Writes the synthetic telemetry the Demo Mode build replays:
 *
 *   public/demo/system-monitor.json  one SnapshotPayload per tick, plus the
 *                                    console events that ride along with them
 *   public/demo/clients.json         one /api/wifi/clients answer
 *   public/demo/device-info.json     the one demo DUT, as /api/duts lists it
 *
 * Nothing here is read from a DUT or a bundle. Every value is generated from a
 * fixed seed, so a rerun reproduces the files byte for byte and a diff of them
 * means the generator changed. Identifiers are synthetic by construction:
 * MACs are locally administered (02:00:5e:…), IPs are TEST-NET-1 (192.0.2.0/24,
 * RFC 5737) and SSIDs say "Demo". That is the whole sanitisation story, and it
 * is why this does not reuse demo/build_demo_data.py's anonymiser: there is
 * nothing real here to anonymise.
 *
 *   node scripts/generate-demo-telemetry.mjs     (or: npm run demo:data)
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "demo");
const RECORDS = 120;
const CORES = 4;
const MEM_TOTAL_KB = 1_036_288;

// mulberry32: tiny, deterministic, good enough for plausible jitter.
function prng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = prng(0x0d0e_2026);
const jitter = (spread) => (rand() * 2 - 1) * spread;
const r1 = (v) => Math.round(v * 10) / 10;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// A load profile with a story: idle, a traffic test ramps up and holds, then
// settles. Reads well on a 120-point trend and loops without a visible seam
// because both ends sit at the same idle baseline.
function loadAt(i) {
  if (i < 30) return 18;
  if (i < 45) return 18 + ((i - 30) / 15) * 52;
  if (i < 80) return 70;
  if (i < 95) return 70 - ((i - 80) / 15) * 52;
  return 18;
}

function cpuBlock(i) {
  const cpu = {};
  const base = loadAt(i);
  for (let core = 0; core < CORES; core += 1) {
    // Core 0 takes the softirq load of the radios, as it does on the real
    // platform, so the per-core legend has something to show.
    const busy = clamp(base + jitter(6) + (core === 0 ? 8 : 0), 2, 98);
    const sirq = r1(clamp(busy * (core === 0 ? 0.35 : 0.12), 0, 40));
    const sys = r1(clamp(busy * 0.3, 0, 60));
    const usr = r1(Math.max(0, busy - sirq - sys));
    cpu[String(core)] = { usr, sys, nic: 0, idle: r1(100 - usr - sys - sirq), io: 0, irq: 0, sirq };
  }
  return cpu;
}

function memoryBlock(i) {
  // Slab creeps up through the traffic test and is reclaimed after it, with
  // load-driven page-cache swings: the shape the CPU / Memory card exists to
  // make visible. Both ends of the loop sit at the same level, so the replay
  // wraps without a step in the trend.
  const creep = Math.sin((Math.PI * i) / RECORDS) * 9_000;
  const sunreclaim = Math.round(38_000 + creep + jitter(150));
  const slab = sunreclaim + Math.round(21_000 + jitter(200));
  const cached = Math.round(142_000 + loadAt(i) * 420 + jitter(500));
  const buffers = Math.round(9_800 + jitter(100));
  const free = Math.round(MEM_TOTAL_KB - 412_000 - cached - slab + jitter(800));
  const available = free + cached + buffers - Math.round(60_000 + jitter(800));
  return {
    MemTotal: MEM_TOTAL_KB,
    MemFree: free,
    MemAvailable: available,
    Buffers: buffers,
    Cached: cached,
    Slab: slab,
    SReclaimable: slab - sunreclaim,
    SUnreclaim: sunreclaim,
  };
}

function clientsAt(i) {
  const load = loadAt(i) > 40 ? 1 : 0;
  return {
    "2G": 4 + (i % 37 < 20 ? 1 : 0),
    "5G": 6 + load + (i % 23 < 8 ? 1 : 0),
    "6G": 1 + load,
  };
}

const mac = (n) => `02:00:5e:10:00:${n.toString(16).padStart(2, "0")}`;

// Console lines that are not part of a snapshot block. `at` is the record
// index they follow. The watchdog lines exist so the Critical Crash panel has
// something to show (they match the default "watchdog" keyword). There are two,
// half a loop apart, because the console keeps its last 1000 lines -- about 50
// ticks of this output -- and one per 120-tick loop would leave the panel
// empty most of the time.
const EVENTS = [
  { at: 5, line: "[  786.402518] wlan: target watchdog: soft lockup recovered on wifi0 (synthetic demo event)" },
  { at: 12, line: "ath1: STA 02:00:5e:10:00:0b IEEE 802.11: associated (demo)" },
  { at: 31, line: "[  812.004113] traffic-gen: demo throughput test started on ath1/ath2" },
  { at: 65, line: "[  846.771902] wlan: target watchdog: soft lockup recovered on wifi1 (synthetic demo event)" },
  { at: 77, line: "ath2: STA 02:00:5e:10:00:12 IEEE 802.11: disassociated (demo)" },
  { at: 81, line: "[  862.118430] traffic-gen: demo throughput test finished" },
  { at: 104, line: "ath1: STA 02:00:5e:10:00:12 IEEE 802.11: associated (demo)" },
];

const records = [];
for (let i = 0; i < RECORDS; i += 1) {
  const counts = clientsAt(i);
  const wifi_clients = {};
  for (const [radio, total] of Object.entries(counts)) {
    wifi_clients[radio] = { total_size: total, clients: [] };
  }
  records.push({ cpu: cpuBlock(i), memory: memoryBlock(i), wifi_clients });
}

const VAPS = [
  { iface: "ath0", ssid: "DemoLab-2G", band: "2.4GHz", channel: 6 },
  { iface: "ath1", ssid: "DemoLab-5G", band: "5GHz", channel: 36 },
  { iface: "ath2", ssid: "DemoLab-6G", band: "6GHz", channel: 37 },
];
const PHY = { "2.4GHz": ["IEEE80211_MODE_11AXG_HE20", "20MHz"], "5GHz": ["IEEE80211_MODE_11AXA_HE80", "80MHz"], "6GHz": ["IEEE80211_MODE_11AXA_HE160", "160MHz"] };
const VENDORS = ["Demo Phone", "Demo Laptop", "Demo Tablet", "Demo IoT", ""];
const clients = [];
let n = 1;
for (const [index, vap] of VAPS.entries()) {
  const count = [5, 7, 2][index];
  for (let k = 0; k < count; k += 1, n += 1) {
    const rssi = Math.round(-38 - rand() * 35);
    const [phymode, width] = PHY[vap.band];
    clients.push({
      iface: vap.iface,
      band: vap.band,
      ssid: vap.ssid,
      mac: mac(n),
      vendor: VENDORS[n % VENDORS.length],
      aid: k + 1,
      channel: vap.channel,
      txrate: `${Math.round(200 + rand() * 1000)}M`,
      rxrate: `${Math.round(150 + rand() * 900)}M`,
      rssi,
      signal_pct: clamp(Math.round(((rssi + 95) / 60) * 100), 1, 100),
      snr: rssi + 95,
      assoc_time: `00:${String(Math.round(rand() * 59)).padStart(2, "0")}:${String(Math.round(rand() * 59)).padStart(2, "0")}`,
      phymode,
      width,
      rxnss: vap.band === "2.4GHz" ? 1 : 2,
      txnss: vap.band === "2.4GHz" ? 1 : 2,
    });
  }
}

const ABOUT =
  "Synthetic telemetry for the DUT Browser public demo. Generated by " +
  "scripts/generate-demo-telemetry.mjs from a fixed seed; no value was read from a device.";

mkdirSync(OUT_DIR, { recursive: true });
const write = (name, payload) =>
  writeFileSync(join(OUT_DIR, name), `${JSON.stringify(payload, null, 1)}\n`);

write("system-monitor.json", { about: ABOUT, interval_ms: 1000, records, events: EVENTS });
write("clients.json", { about: ABOUT, clients, vaps: VAPS });
write("device-info.json", {
  about: ABOUT,
  dut: {
    id: "default",
    label: "Demo AP (synthetic)",
    model: "AP6_DEMO",
    model_cores: CORES,
    device_id: "demo-ap-01",
    vaps_per_band: 1,
    bands: ["2G", "5G", "6G"],
  },
});
console.log(`wrote ${RECORDS} records, ${clients.length} clients to ${OUT_DIR}`);
