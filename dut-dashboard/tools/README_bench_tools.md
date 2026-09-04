# Bench tools

Three diagnostics for driving a real DUT from a script. They are **not tests**:
nothing here runs in CI, none of it asserts, and every one of them needs the
hardware. What they replace is the throwaway script each bench session was
writing from scratch.

| Tool | Question it answers |
|---|---|
| `bench_console.py` | What is on this port — which unit, which model, and what does it say about its mesh? |
| `bench_fleet_e2e.py` | Does the whole Fleet flow hold up across a real mesh, through the shipped endpoints? |
| `bench_identify_load.py` | Does `identify` still work when the console is busy? |

## Why they exist rather than a `curl`

Every DUT route in the app is engineer-gated, so reaching them from a script
means a role passcode and a session. These borrow the app's own `SerialWorker`
and endpoint functions instead: same transports, same parsers, no auth. The only
thing bypassed is FastAPI's role gating, and that has its own coverage in
`backend/tests/test_route_protection.py`.

That also means they exercise the **shipped** code path. `bench_fleet_e2e.py`
calls `main.identify_dut`, `main.probe_dut_mesh` and `fleet_api.capture_rssi`
directly rather than reimplementing what they do — a reimplementation would only
ever prove itself.

## Rules they all follow

- **The console is released in a `finally`.** It admits exactly one process and
  is shared with other sessions; `lsof /dev/cu.PL2303G-* /dev/tty.PL2303G-*`
  before you start. Both nodes, because a `minicom` holds the *tty* twin and
  blocks the `cu.*` node without appearing in a `cu.*`-only listing.
- **Nothing is written outside a temporary directory.** The real
  `logs/duts.json` describes the operator's bench and is not a scratch file.
- **Read-only on the device**, apart from `bench_identify_load.py` starting the
  DUT's own telemetry script, which is what the product does in normal use.

## Two failures worth knowing about before you trust a run

**A console that answers nothing is usually waiting for a login.** The DUT wants
one after every reboot and only a person can give it. `bench_console.py` says so
and exits 2 rather than reporting an empty result as a finding.

**A load test that never loaded the line reports beautiful numbers.** The
throwaway ancestor of `bench_identify_load.py` reported 8/8 successes against a
console its load had never reached: sysMon refuses bare numbers and prints a
usage line, so nothing started. Every attempt returned in 0.0s — identical to
the baseline, and the only tell. So the tool now samples throughput before and
during, and **exits 3 without reporting anything** when the loaded window is not
measurably busier.

Two things that cause exactly that, both found by running it:

- `sh /mnt/data/sysMon001.sh 1 180` starts nothing. The arguments need unit
  suffixes: `1s 180s`.
- `SerialWorker.send` is a raw write and appends no newline, so a command sent
  without one sits on the input line until something else presses Enter. The
  throwaway script got away with it because its next call was a capture, whose
  own newline ran both.

## Running them

```bash
# what is on this port
python3 tools/bench_console.py --port /dev/cu.PL2303G-USBtoUART1130
python3 tools/bench_console.py --ssh-host 192.168.30.124 --ssh-user nelson \
    --ssh-key ~/.ssh/dut_fleet_ed25519 --ssh-device /dev/ttyUSB0
python3 tools/bench_console.py --port /dev/cu.… --interfaces   # adds `iw dev`

# identify while the line is busy
python3 tools/bench_identify_load.py --port /dev/cu.PL2303G-USBtoUART1130
python3 tools/bench_identify_load.py --port /dev/cu.… \
    --load-command "iw dev ath0 scan" --settle 20     # a heavier load than sysMon

# the whole mesh, through the shipped endpoints
python3 tools/bench_fleet_e2e.py --bench bench.json
```

`bench.json` describes the DUTs, and **its order is the capture order — list
nodes before roots**. A root cannot name its own backhaul VAP from its own
console; it is identified from a node's uplink, so a root captured first falls
back to whatever interface is configured. On this bench that fallback once
pointed at a client VAP and drew an ordinary laptop as a mesh child.

```json
[
  {"id": "node1", "label": "Mesh Node (Pi)", "mgmt_url": "https://192.168.30.176",
   "ssh": {"host": "192.168.30.124", "user": "nelson",
           "key_path": "~/.ssh/dut_fleet_ed25519", "device": "/dev/ttyUSB0"}},
  {"id": "root1", "label": "Mesh Root (desk)", "mgmt_url": "https://192.168.30.121",
   "port": "/dev/cu.PL2303G-USBtoUART140"}
]
```

Keep your own `bench.json` outside the repo — it holds a key path and the
addresses of whatever is on your desk, and neither is the same next week.

## What a good run looks like

Recorded 2026-08-29 on the bench these were written against, so a future reader
can tell "the tool is broken" from "the bench changed":

```
bench_console.py --port …UART140
  hostname     : 'AP6420E-PB1005QPCFVFMA8'
  model says   : 2 cores, 8 VAPs/band, bands ['2.4G', '5G', '6G']
  mesh         : True  (members listed)
    192.168.30.121   root  hop=0   …
    192.168.30.176   node  hop=1   …

bench_console.py --ssh-host 192.168.30.124 …          # the same mesh, asked from the node
    192.168.30.121   root  hop=1   …                  # ← the root is hop 1 from here
    192.168.30.176   node  hop=0   …
```

Those two runs are the clearest demonstration of something the parser documents:
**`node` and `hop` are relative to whichever DUT was asked.** Ask the root and it
calls itself hop 0; ask the node and the root becomes hop 1. A hop count out of
this API is not distance from the root.

```
bench_identify_load.py --port …UART1130
   0.0 -> 27.8 lines/s   LOAD CONFIRMED
   loaded : 6/6 identified, 0 learned nothing, 0 other
```
