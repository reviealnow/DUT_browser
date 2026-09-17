"""Seeing a host's SSH key, and trusting one deliberately.

An unknown host key is reported by this dashboard, never accepted for it (see
`serial/pty_ssh.py`), and the operator was told to SSH to the box by hand once
and answer the prompt. That is a terminal trip for something the bench is
already looking at, and -- said plainly -- typing `yes` at that prompt is not a
verification either. Nobody compares the fingerprint unless it is in front of
them.

So this module puts it in front of them. It reads the key the host presents,
says whether this machine already knows one for that address, and trusts a
**named** key on request.

The naming is the whole point and the reason this is not
`StrictHostKeyChecking=accept-new` with extra clicks: `trust` re-reads the host
and writes the key only if its fingerprint is the one the operator confirmed.
A key that changed between the two reads is refused, and so is any host that
already has a different key on record -- a box that was reimaged looks exactly
like a box that was replaced, and only a person can tell those apart.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Where OpenSSH looks, and therefore the only file worth writing.
KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"
SCAN_TIMEOUT_SEC = 5
KEYSCAN = "ssh-keyscan"
KEYGEN = "ssh-keygen"


class HostKeyError(RuntimeError):
    """Something an operator has to decide about, worded for them."""


def _target(host: str, port: int) -> str:
    """How OpenSSH names a host in `known_hosts` — bracketed when not port 22."""
    return host if port == 22 else f"[{host}]:{port}"


def _run(argv: list[str], stdin: str | None = None) -> tuple[str, str]:
    """Run one of OpenSSH's own tools. Returns (stdout, last line of stderr).

    The stderr half is carried rather than dropped for the reason #174 landed
    next door: "No route to host", "Connection refused" and "Connection timed
    out" are three different next moves, and a sentence of our own about
    checking the address covers all three by saying nothing.
    """
    try:
        done = subprocess.run(
            argv, input=stdin, capture_output=True, text=True,
            timeout=SCAN_TIMEOUT_SEC + 5,
        )
    except FileNotFoundError as exc:
        raise HostKeyError(f"{argv[0]} is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise HostKeyError(f"{argv[0]} did not answer in time") from exc
    said = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    return done.stdout, (said[-1] if said else "")


def _fingerprints(lines: list[str]) -> list[dict]:
    """`ssh-keyscan` output -> one entry per key, each with its fingerprint.

    The fingerprint is computed by `ssh-keygen -lf -`, not by us: it is the
    string the operator will compare against what the box prints, and two
    implementations of the same digest is one more thing that can disagree.
    """
    keys: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        listing, _ = _run([KEYGEN, "-lf", "-"], stdin=line + "\n")
        listing = listing.strip()
        # "256 SHA256:abc… host (ED25519)" -> the digest and the type.
        parts = listing.split()
        if len(parts) < 2:
            continue
        keys.append({
            "type": line.split()[1] if len(line.split()) > 1 else "",
            "fingerprint": parts[1],
            "bits": parts[0],
            "line": line,
        })
    return keys


def scan(host: str, port: int = 22) -> list[dict]:
    """What the host presents right now. Never written anywhere by this call."""
    out, said = _run([KEYSCAN, "-T", str(SCAN_TIMEOUT_SEC), "-p", str(port), host])
    keys = _fingerprints(out.splitlines())
    if not keys:
        # ssh-keyscan's own words. It repeats itself once per key type it tried,
        # so one line is the whole of what it had to say.
        raise HostKeyError(
            f"No SSH key came back from {host}:{port}"
            + (f" — ssh-keyscan said: {said}" if said else ".")
        )
    return keys


def known(host: str, port: int = 22) -> list[dict]:
    """What this machine already has on record for that address."""
    out, _ = _run([KEYGEN, "-F", _target(host, port)])
    return _fingerprints([line for line in out.splitlines() if not line.startswith("#")])


def status(host: str, port: int = 22) -> dict:
    """Both halves, as the card needs them: what is on record and what is live.

    A scan that fails is not fatal here -- the record is still worth showing,
    and a box that is off is a different problem from a box whose key changed.
    """
    on_record = known(host, port)
    try:
        presented = scan(host, port)
        scan_error = None
    except HostKeyError as exc:
        presented, scan_error = [], str(exc)
    matched = {k["fingerprint"] for k in on_record} & {k["fingerprint"] for k in presented}
    return {
        "host": host,
        "port": port,
        "known": bool(on_record),
        "known_keys": [{k: key[k] for k in ("type", "bits", "fingerprint")} for key in on_record],
        "presented_keys": [{k: key[k] for k in ("type", "bits", "fingerprint")} for key in presented],
        #: True only when a key on record is one the host is presenting now.
        #: **None means nobody could look** -- the scan failed -- and that is
        #: kept distinct from False on purpose: a host that is known but
        #: presents something else is the case worth stopping for, and
        #: reporting an unreachable box as a changed key would raise exactly
        #: the alarm that must not cry wolf.
        "matches": None if scan_error else bool(matched),
        "scan_error": scan_error,
    }


def trust(host: str, port: int, fingerprint: str) -> dict:
    """Write the key with THIS fingerprint into `known_hosts`, or refuse.

    Three refusals, and each is a different decision that is not ours:

    * the host now presents something else -- either it changed in the seconds
      since the operator read it, or they are answering about a different box;
    * a key is already on record for this address -- a reimaged box and a
      replaced one look identical from here, and overwriting is how the second
      one goes unnoticed;
    * nothing came back at all.
    """
    if not fingerprint.strip():
        raise HostKeyError("No fingerprint was confirmed, so nothing was written.")
    on_record = known(host, port)
    if on_record:
        raise HostKeyError(
            f"{host}:{port} already has a key on record "
            f"({', '.join(k['fingerprint'] for k in on_record)}). Remove it by hand "
            "with `ssh-keygen -R` if the box really was reimaged — this page will "
            "not overwrite a key it did not put there."
        )
    presented = scan(host, port)
    chosen = next((k for k in presented if k["fingerprint"] == fingerprint.strip()), None)
    if chosen is None:
        raise HostKeyError(
            "The host is not presenting that key any more. Nothing was written; "
            "re-read the fingerprint and confirm again."
        )

    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Hashed, as `ssh-keyscan -H` writes them: this file lists every machine
    # somebody reached from here, and it does not need to be readable as an
    # inventory of the bench.
    hashed, _ = _run([KEYGEN, "-H", "-f", "/dev/stdin"], stdin=chosen["line"] + "\n")
    entry = next(
        (line for line in hashed.splitlines() if line.strip() and not line.startswith("#")),
        chosen["line"],
    )
    with open(KNOWN_HOSTS, "a", encoding="utf-8") as handle:
        handle.write(entry.rstrip("\n") + "\n")
    os.chmod(KNOWN_HOSTS, 0o600)
    return {"trusted": chosen["fingerprint"], "type": chosen["type"], "host": host, "port": port}
