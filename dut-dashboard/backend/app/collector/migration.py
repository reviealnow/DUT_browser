"""Folding the remote nodes that already exist into the collector model.

A remote node and a collector were two descriptions of the same bench. A node
said "this DUT's console is `/dev/ttyUSB0` on the Pi at 10.0.0.24, opened with
this key"; a collector says "there is a Pi at 10.0.0.24 you log into". The
second is a fact about the machine and the first is a fact about one port on it,
so a node is really a collector plus a console -- and once that is said out
loud, every node already registered anywhere should be saying it.

**Additive, and nothing is thrown away.** The node's `remote` block keeps every
field it had, `key_path` included, and gains a `collector_id`. So a console
still opens on its own terms if the collector row is ever lost, and this can be
run again on a registry it has already converted without doing anything a second
time. `collector_id` is not one of `CONSOLE_IDENTITY_FIELDS`, so a migrated
node's console identity -- and every backhaul reading filed under it -- survives
untouched.

**It never invents an expected hostname.** Nobody recorded what these boxes call
themselves, and deriving one from the address would either manufacture a
mismatch at the next login or hide a real one.
"""

from __future__ import annotations

import re

from app.collector.registry import AUTH_KEY, MAX_COLLECTORS, CollectorError

_UNSAFE = re.compile(r"[^a-z0-9]+")


def collector_id_for(host: str) -> str:
    """A stable, legal collector id derived from an address.

    Derived rather than random so running this twice, or on two machines with
    the same bench, produces the same id -- which is what makes the whole thing
    repeatable rather than a one-shot script somebody has to be careful with.
    """
    slug = _UNSAFE.sub("-", host.strip().lower()).strip("-")
    if not slug or not slug[0].isalnum():
        slug = f"pi-{slug}".strip("-")
    return slug[:32] or "collector"


def _matching(collectors, remote: dict) -> str | None:
    """An existing collector that is already this login on this box, or None."""
    for collector_id in collectors.ids():
        existing = collectors.get(collector_id)
        if (
            existing.ip == remote["host"]
            and existing.user == remote["user"]
            and existing.port == remote["port"]
            and existing.auth == AUTH_KEY
            and existing.key_path == remote["key_path"]
        ):
            return collector_id
    return None


def _free_id(collectors, wanted: str) -> str:
    """`wanted`, or the first suffixed variant nothing else is using."""
    if wanted not in collectors.ids():
        return wanted
    for suffix in range(2, 100):
        candidate = f"{wanted[:29]}-{suffix}"
        if candidate not in collectors.ids():
            return candidate
    raise CollectorError(f"Could not find a free collector id near {wanted}")


def migrate_remote_nodes(dut_registry, collectors) -> list[dict]:
    """Give every key-authenticated remote node a collector. Returns what it did.

    Best-effort per node: one that cannot be converted -- a full collector
    table, an address the collector model will not accept -- is reported and
    skipped, never allowed to stop the others or to leave a DUT half-changed.
    The node keeps working exactly as it did in that case, because nothing about
    it was removed.
    """
    done: list[dict] = []
    for dut_id in dut_registry.ids():
        remote = dut_registry.get(dut_id).remote
        if not remote or remote.get("collector_id") or not remote.get("key_path"):
            continue
        try:
            collector_id = _matching(collectors, remote)
            if collector_id is None:
                if len(collectors.ids()) >= MAX_COLLECTORS:
                    raise CollectorError(
                        f"Collector limit reached ({MAX_COLLECTORS}); "
                        f"{dut_id} still works, but was not converted"
                    )
                collector_id = _free_id(collectors, collector_id_for(remote["host"]))
                collectors.configure({
                    "id": collector_id,
                    # Named for the box, not for the DUT hanging off it: a
                    # second console on the same Pi joins this same row.
                    "label": f"Collector {remote['host']}",
                    "ip": remote["host"],
                    # Deliberately absent -- see the module docstring.
                    "hostname": None,
                    "user": remote["user"],
                    "port": remote["port"],
                    "auth": AUTH_KEY,
                    "key_path": remote["key_path"],
                })
            dut_registry.configure_remote(dut_id, {**remote, "collector_id": collector_id})
            done.append({"dut": dut_id, "collector": collector_id, "ok": True})
        except (CollectorError, ValueError, KeyError) as exc:
            done.append({"dut": dut_id, "collector": None, "ok": False, "detail": str(exc)})
    return done
