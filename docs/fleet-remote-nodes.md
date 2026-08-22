# Fleet remote nodes — deploying and using them

Monitoring a DUT that is not plugged into the machine running the dashboard. Its
console is reached over SSH to a Raspberry Pi, which pipes the serial port
through `socat`. Everything downstream — the parser, `capture_command`, terminal
mode, the log session — is the same code a locally-cabled DUT uses.

Written against the AP6 420 and AP6 840E on the bench, 2026-08. Every command
here was run; where something was verified indirectly it says so.

## Two axes, kept apart

|  | values | what it decides |
|---|---|---|
| **Console location** | this machine (local serial) · a remote Pi (SSH + `socat`) | the card's colour and its `Remote via host:port` line |
| **Mesh role** | root (no uplink) · node (has one) · neither, so far as anything can tell | the `Uplink to parent` row |

They are independent. A mesh root can hang off a Pi, and one does on this bench;
the fleet's actual root has also been the AP6 cabled to this machine, which is
why the backhaul capture is offered on a cabled DUT and not only on a registered
node. Do not read a card's colour as a topology claim.

---

## 1. On each Pi, once

```bash
sudo apt install -y socat
id -nG | tr ' ' '\n' | grep -qx dialout || sudo usermod -aG dialout "$USER"   # log out and back in
ls -l /dev/ttyUSB*
```

`dialout` membership is what lets the login open the serial device; without it
`socat` fails with `Permission denied` on a device that plainly exists.

**One Pi carries several consoles.** Each node's configuration names its own
`device`, so two DUTs on one Pi is the normal arrangement, not a workaround.

## 2. On the machine running the dashboard, once

Generate a key **with no passphrase**, dedicated to this:

```bash
ssh-keygen -t ed25519 -N "" -C "dut-fleet" -f ~/.ssh/dut_fleet_ed25519
ssh-copy-id -i ~/.ssh/dut_fleet_ed25519.pub <user>@<pi-host>
```

> **The key must have no passphrase.** The backend runs `ssh -o BatchMode=yes`
> as a service: no agent, and nobody to type anything. A passphrase-protected
> key authenticates as far as `Server accepts key` and then fails with
> `Permission denied (publickey,password)` — a message that never mentions the
> real cause. This is not a shortcut; it is the only configuration that works.

Then connect **by hand once**, so a person checks the fingerprint:

```bash
ssh -i ~/.ssh/dut_fleet_ed25519 <user>@<pi-host> 'hostname; command -v socat'
```

Host-key checking is left at its default on purpose, so until the Pi is a known
host every attempt dies with `Host key verification failed.`

## 3. Start the dashboard with an admin passcode

All four fleet routes are admin-only, and a role with no passcode is locked.

```bash
DUT_ADMIN_PASSCODE='<choose one>' ./scripts/start_lan.sh
```

A passcode stored in the workspace database takes precedence over the
environment variable; with neither, the role stays locked. Log in, choose
`admin`, enter the passcode.

## 4. Register each node

**Settings → Fleet remote nodes**, logged in as admin. Fill the form and press
*Register node*; the card lists what is registered and removes one on request.
It is in Settings rather than on the strip because the strip hides itself when
the fleet has one DUT or fewer — which is the state you are in before the first
node exists, so a control there could never add the first one.

Nothing about the form reaches the Pi: it writes the configuration, and the
first SSH attempt is *Connect* on the strip (step 5).

The same registration over the API, for a script — the session cookie comes from
a login:

```bash
curl -X POST http://localhost:8000/api/fleet/nodes -b cookies.txt -H 'Content-Type: application/json' -d '{"id":"node1","label":"Mesh Node (420)","host":"10.0.0.24","user":"pi","key_path":"/home/you/.ssh/dut_fleet_ed25519","port":22,"device":"/dev/ttyUSB0","baudrate":115200,"is_mesh":true,"backhaul_iface":"ath16"}'
```

* `key_path` is a path **on the dashboard's machine**, not on the Pi.
* `host` and `user` must start with an alphanumeric: a value beginning with `-`
  would reach `ssh` as an option rather than a name, and is refused.
* `backhaul_iface` is only a **fallback**. Detection overrides it wherever it
  works; it is consulted for a root, which cannot identify its own backhaul VAP.
* `user` and `key_path` are stored server-side and never appear in `/api/duts`
  — so the card cannot show them back to you either, by construction.
* At most four remote nodes. Re-registering an id already in the table replaces
  its configuration, and stays allowed at the limit.

Register the root the same way, with its own `device`.

## 5. Using it

In the Fleet strip at the top of Overview:

1. **Connect the node first, then the root.** Connecting also runs one capture.
2. **Refresh RSSI** re-reads both directions on demand.
3. **Close serial** asks first, then releases the Pi's serial port.

The order is not a ritual. A root cannot name its own backhaul VAP from its own
console — "the Master VAP with stations" is wrong, because an ordinary client VAP
has stations too. It is identified from **another DUT**: the BSSID a node reports
as its uplink peer *is* the root's backhaul VAP. Capture the root first and it
falls back to whatever `backhaul_iface` was configured, which is a silent empty
list when that guess is wrong.

### The cabled DUT is captured the same way

**Refresh RSSI works on a DUT with no SSH configuration at all.** The capture is
two console commands — `iwconfig` and `wlanconfig <vap> list` — and a serial
console answers them exactly as an SSH one does. This matters because the mesh
root is frequently the DUT cabled to the machine running the dashboard, and
until it was allowed here that was the one device whose backhaul could never be
shown. Nothing needs registering: a cabled DUT with its console open is offered
the button, and it is admin-only like every other `/api/fleet` route.

What a cabled DUT does not carry is the pair of declarations a node's
configuration does — nobody said it is meshed, and nobody named a fallback
`backhaul_iface` — so its capture reports only what it measured:

* an uplink, if it has a parent, exactly as a node's;
* `root`, if it has no parent **and** another DUT's uplink names one of its
  VAPs — which is how a cabled root gets its children;
* neither, if it has no parent and nothing ties it to the mesh. That reads
  `None — no parent found`, not `None — this is the root`: a standalone AP on a
  desk has no parent either, and the two are not the same claim. Capture a node
  that joins it and re-capture; **Capture all** already does this in one press.

There is no `backhaul_iface` fallback for a cabled DUT, and none is wanted: on
this bench the configured value pointed at a client VAP and rendered an ordinary
laptop as a mesh child, while detection from a peer's uplink names the VAP
exactly.

## 6. Reading a card

| row | what it means |
|---|---|
| `Mother server` / `Remote via host:port` | where the console is attached |
| `SSH session` | whether the backend holds an SSH console open. **Not** whether the Pi is reachable — nothing here probes it. Remote nodes only |
| `Node console` | whether telemetry is arriving: `Streaming` / `No DUT` / `Offline`. Remote nodes only |
| `Uplink to parent` | how well this DUT hears its parent. `None — this is the root` is an answer, not a missing measurement |
| `Children on backhaul` | how well it hears each child. A trailing `· athN configured` means that interface came from configuration and nobody verified it is a backhaul at all |

The two backhaul rows are on **every** card, cabled DUTs included; the two SSH
rows are only on a remote node's.

Four things those rows can say, and they are four different statements:

| reading | means |
|---|---|
| `Not captured` | nobody has run a capture on this DUT yet |
| `Not applicable` | an admin registered this node with `is_mesh:false` |
| `None — this is the root` | measured: no parent, and this DUT is in the mesh |
| `None — no parent found` / `No backhaul VAP identified` | measured: no parent, and nothing yet ties this DUT to a mesh. Either it is standalone, or no node that joins it has been captured |

## 7. When it fails

| symptom | cause |
|---|---|
| `Host key verification failed.` | the Pi is not in `known_hosts` — connect by hand once |
| `Permission denied (publickey,password)`, key installed | the key has a passphrase |
| `Remote Pi is missing socat; install socat on the Pi and reconnect` | exactly that |
| `socat … open("/dev/ttyUSBn"): Permission denied` | the login is not in `dialout`, or the device path is wrong |
| Connect returns ok, the node drops seconds later | SSH succeeded and `socat` failed after it. The console-liveness row is what shows this |
| `Uplink to parent` stays `Not captured` | no capture has run: the console is not open, or nobody pressed the button. A capture that ran and found no parent says so in words instead |
| `Children on backhaul` empty **and** marked `configured` | the configured interface is not the backhaul. Capture a node, then re-capture the root |

**The serial port admits one process.** A `minicom` on the Pi will keep `socat`
from opening the device, and vice versa. After a session, confirm the port is
free:

```bash
ssh -i ~/.ssh/dut_fleet_ed25519 <user>@<pi-host> 'pgrep -a socat || echo free'
```

The DUT's own console also needs a human login after any reboot; an agent cannot
do it. Release the port, let someone log in, and take it back once the prompt
reads `AP6_840E#`.

## What the two directions actually measure

`wlanconfig <vap> list` only answers **downward**: it lists the stations
associated to a Master VAP. A node's link **up** to its parent lives on its
Managed VAP and is visible only through `iwconfig`. Asking `wlanconfig` for an
uplink returns an empty table and no error, which is why the two are separate
rows fed by separate commands.

The pair is identified by structure, not by name: the uplink is the `Managed` VAP
with a live link quality, and the downlink is the `Master` VAP sharing its ESSID
and band. Interface numbers differ between models — `ath14`/`ath15` on an AP6 420,
`ath22` on an 840E — and SSIDs are operator-chosen and change.

A sanity check worth running once: the same link read from both ends should agree
within a few dB. On this bench the node heard the root at -37 dBm while the root
heard the node at -38 dBm.
