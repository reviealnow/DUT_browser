# Edge log collectors

A collector is a machine on the bench that somebody logs into over SSH with a
password — in practice the Raspberry Pi. It is **not** a DUT and not a DUT's
console, and it has its own registry for that reason.

The topology it sits in:

```
LAN DUT console        <<Server>>          LAN DUT console
(cabled to this        Raspberry Pi        (reached from the Pi)
 machine; the serial   the collector
 console the dashboard    ⇅ ssh
 already shows)
```

**Remote nodes are collectors now.** `docs/fleet-remote-nodes.md` described the
same bench in different words: a node said "this DUT's console is
`/dev/ttyUSB0` on the Pi at 10.0.0.24, opened with this key", which is a fact
about a machine and a fact about one port on it, welded together. Every node
already registered is converted on startup, and a node registered through
`/api/fleet/nodes` today is registered as a collector plus a console in the same
request. Two DUTs on one Pi — which that guide calls the normal arrangement —
are now two consoles on one collector rather than two rows repeating the same
host, user and key.

## Why it is not part of the DUT registry

`dut/registry.py` describes devices under test: each one is a parser, a serial
worker, a snapshot ring and a console buffer, and a node's `remote` block names
a *console location* — which Pi holds which `/dev/ttyUSB`, so telemetry can be
parsed out of it. A collector has none of those. Folding it in would have meant
a DUT-shaped record with five of its seven parts permanently empty, and a fleet
list that mixes devices with the machines that reach them.

## Two ways to log in

A merged model that could not express what it replaced would not be a merge, so
a collector authenticates either way:

| | how | where the credential lives |
|---|---|---|
| **password** | typed at a controlling terminal | this process's memory, nowhere else |
| **key** | `ssh -o BatchMode=yes -i <path>` | a file on this machine, named in `collectors.json` |

A key path is not a secret and is persisted exactly as a remote node's always
was: it names a file, and the file never travels. A password is not persisted at
all. `ready` is the field that says whether a login is possible *right now* —
always true for a key, false for a password after a restart — and it is what the
card gates its Connect button on. `has_password` stays a separate answer to a
separate question.

Everything downstream — the held session, the console probe, attaching a DUT
console — asks the registry which it is rather than assuming, so exactly one
place decides.

## Two decisions that were reversed to make the merge possible

Both were mine, both had real reasons, and both cost more than they bought once
existing nodes had to fit:

* **`ip` accepted only an IP address**, so that DNS was never between the
  operator and the box they picked. Given up: a remote node's host has always
  been allowed to be a name, this guide writes it as `<pi-host>`, and a Pi's
  default name on a bench is `raspberrypi.local`. `ip` and `hostname` stay
  different fields because they have different **jobs** — one is dialled, the
  other is checked against what the box answers — not different shapes.
* **`hostname` was required.** Now optional, because a collector derived from a
  remote node carries nobody's expectation of what the box calls itself.
  Deriving one from the address would either manufacture a mismatch at the next
  login or hide a real one, so absent stays absent and the mismatch check simply
  does not run.

## What the migration does, and what it refuses to do

`collector/migration.py`, run at startup, after both registries load.

For every DUT whose `remote` names a `key_path` and no collector: find a
collector that is already that login on that box, or create one, and write its
id onto the node's remote. The id is **derived** from the address —
`10.0.0.24` → `10-0-0-24`, `pi-node-1.local` → `pi-node-1-local` — so running it
twice, or on two machines sharing a bench, produces the same row.

**Additive. Nothing is removed.** The node keeps every field it had, `key_path`
included, so its console still opens on its own terms if the collector row is
ever lost — and `connect_node` falls back to exactly that. Running it again on a
converted registry does nothing.

**`collector_id` is deliberately not one of `CONSOLE_IDENTITY_FIELDS`**, so a
migrated node's console identity does not move and every backhaul reading filed
under it stays valid. There is a test that says so out loud, because the failure
would be silent: a reading whose console identity changed underneath it is
discarded the next time anything asks.

**A node it cannot convert is reported and left working.** A full collector
table, or an address the collector model will not take, logs a warning and skips
that node — never stops the others, never leaves one half-changed. A boot that
half-converts a bench is worse than one that converts none of it.

## The password rule

**Passwords live in memory and nowhere else.** They are never written to
`logs/collectors.json`, never returned by any endpoint, and never logged. A
backend restart therefore keeps every collector and forgets every login; the
API reports this as `has_password: false` and the card asks for the password
again rather than offering a Connect that could only fail.

`tests/test_collector_registry.py` asserts this against the bytes actually
written to the state file, not against intent.

## Why a pty

`ssh` reads a password from its **controlling terminal**, never from stdin, so
a child with a pipe on stdin cannot answer the prompt however carefully it
writes to it. The DUT-console transport avoids the question entirely by running
`ssh -o BatchMode=yes` with a key — BatchMode means "never prompt" — and that
is still the right transport for a console.

`collector/ssh_session.py` uses `pty.fork()`: it forks, makes the child a
session leader, and gives it the pty slave as its controlling tty. The child
does nothing between fork and `exec`, because this process has threads in it
and a forked copy inherits locks no surviving thread will release.

Three things that module refuses to do:

* **Accept a host key for you.** An unknown host makes ssh ask, on that same
  terminal. The question is detected and reported; answering it would be the
  dashboard deciding to trust an unidentified machine. SSH to a new collector
  by hand once first.
* **Let a key stand in for the password.** `PubkeyAuthentication=no` and an
  explicit `PreferredAuthentications` are set, so an agent key already loaded
  on this machine cannot quietly satisfy a login the operator was told was
  checked against their password.
* **Put the password in an error.** Real ssh turns echo off while reading, so
  normally there is nothing to leak; where a terminal echoes anyway, the value
  is scrubbed before anything is raised.

## The session, and the light

Connect holds the SSH session **open** — the remote command announces the box's
own hostname and then `exec cat`s. While that process lives, the collector is
connected; the card breathes a green dot beside the word *Connected*.

The dot moves because a static green one reads exactly the same whether the
session is alive or died ten minutes ago. It is `aria-hidden`, the word beside
it is what a screen reader gets, and `prefers-reduced-motion` stops the motion
— so colour and text have to carry the state on their own.

`connected` is asked of the process table on every read, never cached. The
Settings card re-reads it every 5s.

## Hostname vs IP

Both are registered, and they do different jobs:

* `ip` must parse as an IP address. The backend refuses a name, so DNS is never
  between the operator and the box they picked.
* `hostname` is what that box is expected to call itself. It is read back at
  login and compared. A mismatch is **reported, not fatal**: the login worked,
  but this is a different machine than the operator believes — which is exactly
  the thing a log collector must not be wrong about.

## The API

Every route is admin, gated in the router like `fleet_api`, and listed in
`tests/test_route_protection.py`'s `ROLE_MAP`.

| method | path | what it does |
|---|---|---|
| `GET` | `/api/collectors` | list, with live status. Never a password |
| `POST` | `/api/collectors` | register or re-configure. `password` optional |
| `POST` | `/api/collectors/{id}/password` | hand back a password a restart forgot |
| `POST` | `/api/collectors/{id}/connect` | log in and hold the session |
| `POST` | `/api/collectors/{id}/disconnect` | drop the session |
| `DELETE` | `/api/collectors/{id}` | drop the registration. Nothing on the box is touched |

`400` means this side is not ready and nothing was sent — no password is held.
`502` means the dashboard did its part and the collector refused, was
unreachable, or is unidentified.

At most eight collectors. Re-posting an existing id re-configures it and stays
allowed at the limit. Re-pointing one at a different address or login drops the
password and session it was holding; editing only its label does not.

## The DUT consoles behind a collector

This is the right-hand half of the topology. The collector is reached over SSH;
the DUTs are on **its** serial ports.

### Finding them

`GET /api/collectors/{id}/consoles` runs four questions over the session that is
already open, so it costs no login and no serial time. They are the four things
that stop a console from opening, and until now `docs/fleet-remote-nodes.md`
made somebody SSH in by hand to check each one:

| question | how |
|---|---|
| which serial devices exist | `ls -1 /dev/ttyUSB* /dev/ttyACM*` |
| is `socat` installed | `command -v socat` |
| can the login open a device | `id -nG`, looking for `dialout` |
| is a port already busy | `fuser <device>` |

**A port whose state could not be read is reported as unread, never as free.**
`fuser` is not on every image; without it every device's `busy` is `null` and
the card says *In use? Not checked*. Reporting an unchecked port as free is how
a bench spends an afternoon on a `socat` that is quietly losing to a `minicom`
somebody left running the previous day.

`busy` and `attached_dut` are different facts and the card keeps them apart:
one is whatever the box says has the port, the other is a console **this
dashboard** is holding. The first means go and look; the second means press
*Detach*.

Device names are filtered against the same expression the DUT registry accepts
before they are shown or used, because each one ends up interpolated into a
`socat` command line on the far end.

### Attaching one

**This is the only place consoles are registered now.** The *Fleet remote nodes*
card in Settings was retired with the merge; the two declarations it carried
that nothing can measure — whether the DUT is in a mesh, and the fallback
backhaul VAP — are asked for **per port**, on the row you are attaching. Per
port and not per panel on purpose: one toggle governing whichever device you
press next is how the wrong console ends up declared meshed, and a wrongly
meshed DUT does not fail, it reports a backhaul capture that is simply untrue.

Registration also offers both authentication modes, so a Pi that only takes a
key is registered here rather than in the card that used to exist for it.


`POST /api/collectors/{id}/consoles/attach` with a device and a baud rate opens
an **ordinary DUT**: same parser, same snapshot ring, same console buffer, same
log session, appearing in the switcher like any other. Only the transport
differs, and only in how it authenticates.

The DUT id is derived — `edge1` + `/dev/ttyUSB0` → `edge1-ttyusb0` — so
re-attaching the same physical port lands on the DUT that already has its
history, and two ports on one Pi cannot collide.

What is persisted for that DUT names the collector and **not** the password:

```json
{"host": "192.168.30.122", "user": "dut", "key_path": "",
 "collector_id": "edge1", "port": 22, "device": "/dev/ttyUSB0",
 "baudrate": 115200, "is_mesh": false, "backhaul_iface": null}
```

The password is fetched from the collector registry's memory at the moment
somebody presses Attach and handed straight to the transport. `_clean_remote`
builds its result from named fields rather than copying what it was given, so a
password cannot reach `duts.json` even if a caller passes one — asserted in
`tests/test_fleet_remote.py`, at that layer, not only through the API.

`is_mesh` is `false`: nobody has said this DUT is meshed, and a backhaul capture
on a DUT that is not is a wrong answer rather than a missing one.

### How the console transport authenticates

`SerialWorker._open_ssh` now has two ways in, and everything after the login is
identical for both — the child is a plain `Popen` with real pipes either way, so
the readiness handshake, the reader loop and `close` never learn which was used.

* **A key**, with `BatchMode=yes`: a node registered by hand. Unchanged.
* **A password**, for a console behind a collector: spawned with a pty attached
  for the prompt alone.

That split is the point of `serial/pty_ssh.py`. The pty carries the password
question and nothing else; a console's bytes stay on an ordinary pipe, because a
pty line discipline would translate CR/NL and act on control characters in the
middle of a firmware transfer. Measured before it was written: with this split,
a `\x00\x01…\xff` payload arrives on stdout byte for byte while the prompt
arrives on the terminal and ssh's own errors stay on stderr.

One bug worth recording, because it only ever shows up as a hang. A child that
took a pty as its controlling terminal **cannot finish exiting while that
terminal is unread**: a login that failed after the prompt sat in `ps` state
`?Es`, `poll()` answered `None` forever, and the caller reported its own timeout
instead of the reason ssh had already printed. The terminal is therefore drained
from the moment the password is typed, not from when the session object is
built. `tests/test_collector_ssh.py` asserts it on the clock, because the clock
is what actually broke.

## What has not been verified

Everything above is exercised by `tests/test_collector_ssh.py` against a fake
`ssh` that opens `/dev/tty` and prompts exactly as the real one does — which is
what makes the pty mechanism testable at all. **No part of this has yet been
run against a real Raspberry Pi on the bench.** The first person to do so
should confirm, in this order:

1. an unknown host key is reported rather than accepted;
2. a wrong password fails once, quickly, rather than hanging on three prompts;
3. the session survives a few minutes idle, and the light stops within 5s of
   the collector being powered off;
4. the console scan agrees with what `ls /dev/ttyUSB*`, `command -v socat`,
   `id -nG` and `fuser` say when run by hand on the same box;
5. an attached console streams telemetry the parser recognises, and *Detach*
   releases the port — `pgrep -a socat` on the collector should come back empty.

### Before the first attempt, on the collector

Host-key checking is at its default and this dashboard will not answer the
question for you, so connect by hand once and check the fingerprint:

```bash
ssh dut@192.168.30.122 'hostname; command -v socat; id -nG; ls -l /dev/ttyUSB*'
```

That one command answers everything the scan will ask, which makes it the
comparison for point 4 above. Anything missing is fixed on the Pi:

```bash
sudo apt install -y socat psmisc
sudo usermod -aG dialout "$USER"   # then log out and back in
```

`psmisc` is what provides `fuser`. Without it the scan still works and simply
reports every port's busy state as unchecked.
