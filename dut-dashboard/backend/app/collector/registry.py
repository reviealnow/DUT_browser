"""The edge log collectors, kept apart from the DUT registry on purpose.

A collector is not a DUT and not a DUT's console. `dut/registry.py` describes
devices under test -- each one a parser, a serial worker, a snapshot ring and a
console buffer -- and a node's `remote` block is a *console location*: which Pi
holds which `/dev/ttyUSB`, so that telemetry can be parsed out of it. A
collector is the Pi itself, as a machine somebody logs into. It has no parser,
no snapshots and no console of its own, and folding it into a DutContext would
have meant a DUT-shaped record with five of its seven parts permanently empty.

The bench topology this serves::

    LAN DUT console  <<Server>>  Raspberry Pi  (ssh)  >>  LAN DUT console
    (cabled to this machine,      the collector
     the serial console the
     dashboard already shows)

so the same physical Pi can be both a collector here and the host of a remote
node's console over in the DUT registry. They are two statements about it and
neither implies the other: a collector that is reachable says nothing about
whether `socat` can open a serial device on it.

**Two ways to authenticate, because a merge that cannot express what it
replaced is not a merge.** A collector registered by hand takes a password; a
collector derived from an existing remote node takes the key file that node
already named. Everything downstream -- the held session, the console probe,
attaching a DUT console -- is identical either way and asks the registry which
it is rather than assuming.

**The password is never persisted.** It lives in ``_passwords`` for as long as
this process does, is never written to ``COLLECTORS_FILE``, and is never part
of any response body. A restart therefore keeps the collector and forgets how
to log into it -- deliberately, and the API says so with ``ready`` so the UI can
ask for it again rather than presenting a Connect button that can only fail.
A key path is not a secret and is persisted, exactly as it always was on a
remote node: it names a file on this machine, and the file never travels.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass

from app.collector import ssh_session
from app.collector.ssh_session import CollectorSession, CollectorSshError
from app import config

MAX_COLLECTORS = 8
PORT_MIN, PORT_MAX = 1, 65535

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# The name the box calls itself. Leading alphanumeric for the same reason the
# DUT registry's tokens are: a value starting with "-" reaching a command line
# is an option, not a name.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# Public, unlike the two above: `services/fleet_profile_service.py` validates a
# saved host setting against these same expressions, because a profile that
# cannot be applied to a collector is a trap that only springs at Verify time.
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@%+-]*$")
#: What ssh may be asked to dial. Deliberately the same expression the DUT
#: registry has always accepted for a remote node's host -- see `_clean` for why
#: this is looser than it first was.
ADDRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@%+-]*$")
AUTH_KEY = "key"
AUTH_PASSWORD = "password"
MAX_LABEL_LEN = 48


class CollectorError(ValueError):
    """A configuration this registry will not accept, worded for the operator."""


@dataclass
class Collector:
    """One registered collector. Everything here is safe to hand to a client."""

    id: str
    label: str
    #: Where ssh dials. May be an address or a resolvable name; what makes it
    #: different from `hostname` is its job, not its shape -- this is what is
    #: connected to, that is what the box is expected to answer.
    ip: str
    #: What this box is expected to call itself, or None when nobody said.
    #: Checked at login against what it actually answers -- see `connect`.
    hostname: str | None
    user: str
    port: int = 22
    #: How the login is authenticated: a password held in memory, or a key file
    #: on this machine. Both reach the same transport.
    auth: str = AUTH_PASSWORD
    #: Only for `auth == AUTH_KEY`. A path on the dashboard's machine, never a
    #: secret in itself, and persisted exactly as a remote node's always was.
    key_path: str | None = None


def _clean(payload: dict) -> Collector:
    """Validate one collector, naming the field that is wrong.

    Shared by the API body and by the file on disk so the two cannot drift into
    disagreeing about what is acceptable -- the same reason `_clean_remote`
    exists next door in the DUT registry.
    """
    def text(key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CollectorError(f"{key} is required")
        return value.strip()

    collector_id = text("id")
    if not _ID_RE.fullmatch(collector_id):
        raise CollectorError("id must be lowercase letters, digits, '-' or '_'")

    # An address, and a name is allowed. This started out as "an IP address, so
    # DNS is never between the operator and the box they picked" -- a real
    # benefit, and it was given up on purpose. A remote node's host has always
    # been allowed to be a name, `docs/fleet-remote-nodes.md` writes it as
    # `<pi-host>`, and the default name of a Raspberry Pi on a bench is
    # `raspberrypi.local`. A merged model that could not express what the old
    # one did would strand exactly those configurations.
    ip = text("ip")
    if not ADDRESS_RE.fullmatch(ip):
        raise CollectorError("ip must be an address or a host name")

    # Optional, and absent is a real state rather than a gap to fill in. It is
    # what the box is EXPECTED to call itself, checked against what it answers;
    # a collector derived from an existing remote node has nobody's expectation
    # recorded, and inventing one from the address would manufacture a mismatch
    # or hide a real one.
    hostname = payload.get("hostname")
    hostname = hostname.strip() if isinstance(hostname, str) and hostname.strip() else None
    if hostname is not None and not _HOSTNAME_RE.fullmatch(hostname):
        raise CollectorError("hostname must be a host name")

    user = text("user")
    if not USER_RE.fullmatch(user):
        raise CollectorError("user must be an SSH login name")

    port = payload.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise CollectorError(f"port must be between {PORT_MIN} and {PORT_MAX}")

    key_path = payload.get("key_path")
    key_path = key_path.strip() if isinstance(key_path, str) and key_path.strip() else None
    auth = payload.get("auth") or (AUTH_KEY if key_path else AUTH_PASSWORD)
    if auth not in (AUTH_KEY, AUTH_PASSWORD):
        raise CollectorError(f"auth must be '{AUTH_KEY}' or '{AUTH_PASSWORD}'")
    if auth == AUTH_KEY and not key_path:
        raise CollectorError("key_path is required for key authentication")
    if auth == AUTH_PASSWORD:
        # Not merely ignored: a stored key path on a password collector would
        # make `ssh -i` reachable from a row whose UI never mentions a key.
        key_path = None

    label = payload.get("label")
    label = (
        label.strip()[:MAX_LABEL_LEN]
        if isinstance(label, str) and label.strip()
        else (hostname or ip)
    )

    return Collector(
        id=collector_id, label=label, ip=ip, hostname=hostname,
        user=user, port=port, auth=auth, key_path=key_path,
    )


class CollectorRegistry:
    """Registered collectors, their in-memory passwords, and their live sessions."""

    def __init__(self, state_file=None) -> None:
        # Resolved here rather than as a default argument, which would bind the
        # path at import time and make it impossible to redirect afterwards --
        # `mock.patch.object(module, "COLLECTORS_FILE", ...)` would appear to
        # work and the registry would go on writing to the real bench file.
        # Measured, not theorised: a boot-path check written that way wrote a
        # synthetic collector into `logs/collectors.json` on this machine. The
        # DUT registry looks its own path up at call time for the same reason.
        self._override = state_file
        self._lock = threading.RLock()
        self._collectors: dict[str, Collector] = {}
        self._passwords: dict[str, str] = {}
        self._sessions: dict[str, CollectorSession] = {}
        #: Why the last connect attempt ended the way it did, per collector.
        #: Kept so a card can say what went wrong after the request that learned
        #: it has been answered and forgotten.
        self._detail: dict[str, str] = {}

    @property
    def _file(self):
        """Where state is persisted: an explicit override, or today's config."""
        return self._override if self._override is not None else config.COLLECTORS_FILE

    # ---------- configuration ----------

    def configure(self, payload: dict, password: str | None = None) -> Collector:
        """Register or re-configure one collector. Returns the stored record.

        Re-configuring an id that already exists is an edit, and stays allowed at
        the limit; only a genuinely new one is refused when the table is full.
        """
        collector = _clean(payload)
        with self._lock:
            if collector.id not in self._collectors and len(self._collectors) >= MAX_COLLECTORS:
                raise CollectorError(f"Collector limit reached ({MAX_COLLECTORS})")
            existing = self._collectors.get(collector.id)
            if existing is not None and (
                existing.ip != collector.ip
                or existing.user != collector.user
                or existing.port != collector.port
                or existing.auth != collector.auth
                or existing.key_path != collector.key_path
            ):
                # Re-pointing an id at another box, or another login on it, makes
                # the held session and the remembered password somebody else's.
                # Dropping them is the honest move: the alternative is a green
                # light on a card that now describes a machine it is not
                # connected to.
                self._disconnect_locked(collector.id)
                self._passwords.pop(collector.id, None)
            self._collectors[collector.id] = collector
            if password is not None:
                self._set_password_locked(collector.id, password)
            self._save_locked()
            return collector

    def _set_password_locked(self, collector_id: str, password: str) -> None:
        if not password:
            # An empty string is not a password; storing it would make
            # `has_password` claim a login is ready that ssh will reject.
            self._passwords.pop(collector_id, None)
            return
        self._passwords[collector_id] = password

    def set_password(self, collector_id: str, password: str) -> None:
        with self._lock:
            if collector_id not in self._collectors:
                raise KeyError(collector_id)
            self._set_password_locked(collector_id, password)

    def forget_password(self, collector_id: str) -> None:
        with self._lock:
            self._passwords.pop(collector_id, None)

    def remove(self, collector_id: str) -> None:
        with self._lock:
            if collector_id not in self._collectors:
                raise KeyError(collector_id)
            self._disconnect_locked(collector_id)
            self._passwords.pop(collector_id, None)
            self._detail.pop(collector_id, None)
            del self._collectors[collector_id]
            self._save_locked()

    def get(self, collector_id: str) -> Collector:
        with self._lock:
            return self._collectors[collector_id]

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._collectors)

    # ---------- sessions ----------

    def connect(self, collector_id: str) -> dict:
        """Log in and hold the session open. Raises for anything that stopped it."""
        with self._lock:
            collector = self._collectors[collector_id]
            password = ""
            if collector.auth == AUTH_PASSWORD:
                password = self._passwords.get(collector_id, "")
                if not password:
                    raise CollectorError(
                        "No password is held for this collector. Passwords are kept in "
                        "memory only, so a restart forgets them -- enter it again."
                    )
            # Outside the lock would be kinder to other callers, but a second
            # connect arriving mid-login would start a second ssh child and leak
            # the first. A login is bounded by LOGIN_TIMEOUT_SEC, which is the
            # longest this can hold.
            self._disconnect_locked(collector_id)
            try:
                session = ssh_session.open_session(
                    ip=collector.ip,
                    user=collector.user,
                    password=password,
                    key_path=collector.key_path,
                    port=collector.port,
                )
            except CollectorSshError as exc:
                self._detail[collector_id] = str(exc)
                raise
            self._sessions[collector_id] = session
            reported = session.reported_hostname
            # Nothing to compare against is not a mismatch. A collector migrated
            # from a remote node carries nobody's expectation, and reporting
            # that as a disagreement would cry wolf on every one of them.
            matches = collector.hostname is None or reported == collector.hostname
            self._detail[collector_id] = (
                "" if matches else
                f"Logged in, but the box calls itself {reported!r}, "
                f"not {collector.hostname!r}."
            )
            return {
                "connected": True,
                "reported_hostname": reported,
                # Not fatal, and not hidden either. The address is what was
                # connected to and the name is what was expected of it; the two
                # disagreeing means the operator is looking at a different
                # machine than they think, which is exactly the thing a log
                # collector must not be wrong about.
                "hostname_matches": matches,
                "detail": self._detail[collector_id],
            }

    def session_for(self, collector_id: str) -> CollectorSession:
        """The live session, or a refusal worded for the operator.

        Everything that asks a collector a question goes through here rather
        than reaching into `_sessions`, so "connect it first" is one sentence in
        one place instead of a KeyError at four call sites.
        """
        with self._lock:
            session = self._sessions.get(collector_id)
        if session is None or not session.alive():
            raise CollectorError(
                "This collector is not connected. Press Connect first."
            )
        return session

    def credentials_for(self, collector_id: str) -> dict:
        """How to log into this collector, for a transport that is about to.

        Deliberately not part of `status` and not returned by any route: this is
        an internal handoff from the registry to the transport, and it exists so
        a password never has to be persisted on a DUT's remote configuration.

        The shape is what `SerialWorker.open(ssh=...)` reads, so a caller
        forwards it rather than deciding anything about authentication itself --
        which is the whole point of one model instead of two.
        """
        with self._lock:
            collector = self._collectors[collector_id]
            if collector.auth == AUTH_KEY:
                return {"key_path": collector.key_path, "password": ""}
            password = self._passwords.get(collector_id)
        if not password:
            raise CollectorError(
                "No password is held for this collector. Passwords are kept in "
                "memory only, so a restart forgets them -- enter it again."
            )
        return {"key_path": "", "password": password}

    def _disconnect_locked(self, collector_id: str) -> None:
        session = self._sessions.pop(collector_id, None)
        if session is not None:
            session.close()

    def disconnect(self, collector_id: str) -> None:
        with self._lock:
            if collector_id not in self._collectors:
                raise KeyError(collector_id)
            self._disconnect_locked(collector_id)
            self._detail[collector_id] = ""

    def close_all(self) -> None:
        """Reap every held session. For process shutdown."""
        with self._lock:
            for collector_id in list(self._sessions):
                self._disconnect_locked(collector_id)

    def status(self, collector_id: str) -> dict:
        """What a card needs, and nothing a password could be recovered from."""
        with self._lock:
            collector = self._collectors[collector_id]
            session = self._sessions.get(collector_id)
            # Asked, not remembered: the ssh child can die at any moment and the
            # only thing that knows is the process table. A cached boolean here
            # is how a card ends up breathing green at a session that is gone.
            connected = session is not None and session.alive()
            if session is not None and not connected:
                self._sessions.pop(collector_id, None)
                self._detail[collector_id] = "The SSH session ended."
            return {
                "id": collector.id,
                "label": collector.label,
                "ip": collector.ip,
                "hostname": collector.hostname,
                "user": collector.user,
                "port": collector.port,
                "auth": collector.auth,
                # A path, not a secret, and shown for the same reason the fleet
                # card shows a node's device: it is what an operator checks when
                # a login fails.
                "key_path": collector.key_path,
                "connected": connected,
                # Whether a login is even possible right now. False for a
                # password collector after a restart, which is what makes the
                # memory-only rule visible instead of surprising; always true
                # for a key, whose file outlives this process.
                "ready": (
                    collector.auth == AUTH_KEY or collector_id in self._passwords
                ),
                "has_password": collector_id in self._passwords,
                "reported_hostname": session.reported_hostname if connected else None,
                "connected_since": (
                    time.strftime("%H:%M:%S", time.localtime(session.opened_at))
                    if connected else None
                ),
                "detail": self._detail.get(collector_id) or None,
            }

    def list_status(self) -> list[dict]:
        return [self.status(collector_id) for collector_id in self.ids()]

    # ---------- persistence (never the password) ----------

    def _save_locked(self) -> None:
        entries = [
            {
                "id": c.id, "label": c.label, "ip": c.ip,
                "hostname": c.hostname, "user": c.user, "port": c.port,
                "auth": c.auth, "key_path": c.key_path,
            }
            for c in self._collectors.values()
        ]
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(entries), encoding="utf-8")
        except OSError:
            pass  # best-effort, exactly as the DUT registry treats its own file

    def load_persisted(self) -> None:
        """Re-create saved collectors, skipping any entry that no longer validates."""
        try:
            entries = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(entries, list):
            return
        with self._lock:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    collector = _clean(entry)
                except CollectorError:
                    continue
                if len(self._collectors) >= MAX_COLLECTORS:
                    break
                self._collectors[collector.id] = collector
