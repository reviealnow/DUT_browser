#!/usr/bin/env python3
"""A stand-in for `ssh` that behaves like it where this project cares.

Three behaviours are modelled, and each one is load-bearing:

* the password is read from the **controlling terminal**, not from stdin, so a
  transport that writes to the child's stdin hangs here instead of passing;
* every inherited descriptor is closed first, as `closefrom()` does in ssh, so
  a transport that does not hold the pty slave open loses the terminal here
  exactly as it does with the real thing;
* on success it `exec`s the remote command it was given, so the command string
  the caller actually builds is the one under test -- including which of its
  parts go to stdout and which to stderr;
* failures land on **stderr**, where ssh puts them, rather than on the terminal.

Driven by the environment so one script covers every case:

  FAKE_SSH_MODE      ok | denied | hostkey | echo | silent | unreachable
                     (default: ok)
  FAKE_SSH_PASSWORD  what counts as correct        (default: correct)
  FAKE_SSH_DENIAL    the refusal line to print     (default: ssh's usual one)
  FAKE_SSH_ANSWERED  a path written to iff this script is ever answered

`echo` is the abnormal remote: one that repeats what it was sent, putting the
password on a stream the caller is parsing. Real ssh turns echo off, so this
exists only to prove the caller scrubs before it reports.
"""

import os
import sys
import time

MODE = os.environ.get("FAKE_SSH_MODE", "ok")
EXPECTED = os.environ.get("FAKE_SSH_PASSWORD", "correct")
# Configurable because real ssh has three of these and they mean three
# different things; a test that pinned one wording would be asserting on a
# foreign program's words again.
DENIAL = os.environ.get("FAKE_SSH_DENIAL", "Permission denied, please try again.")
ANSWERED = os.environ.get("FAKE_SSH_ANSWERED")


def main() -> int:
    if MODE == "unreachable":
        # A connection that never happened: ssh says why on stderr and exits
        # without ever opening a terminal, so there is no prompt to answer.
        # Its own words are the useful ones -- "No route to host", "Host key
        # verification failed" -- and the caller must surface them rather than
        # inventing a message of its own.
        sys.stderr.write("ssh: connect to host 10.0.0.9 port 22: No route to host\n")
        sys.stderr.flush()
        return 255

    if MODE == "silent":
        # Reachable, alive, and never says anything -- a box that accepted the
        # TCP connection and then stopped. The caller must give up on its own.
        time.sleep(30)
        return 255

    # Real ssh runs `closefrom()` before it asks for anything, dropping every
    # descriptor it did not open itself -- including the pty slave it inherited.
    # Modelling that is what lets this fixture FAIL: without it the fake held
    # the last reference to its own controlling terminal, so every test passed
    # while no real login could be made (2026-09-14).
    os.closerange(3, 256)
    try:
        tty = open("/dev/tty", "r+b", buffering=0)
    except OSError as exc:
        # What ssh does when it cannot ask: no authentication request is sent
        # at all, and the server logs only a connection closed at preauth.
        sys.stderr.write(f"read_passphrase: can't open /dev/tty: {exc.strerror}\n")
        sys.stderr.write("dut@10.0.0.9: Permission denied (publickey,password).\n")
        sys.stderr.flush()
        return 255

    if MODE == "hostkey":
        tty.write(
            b"The authenticity of host '10.0.0.9' can't be established.\r\n"
            b"ED25519 key fingerprint is SHA256:zzz.\r\n"
            b"Are you sure you want to continue connecting (yes/no/[fingerprint])? "
        )
        # Blocks. Anything arriving here means the caller answered a host-key
        # question on the operator's behalf, which is the thing that must not
        # happen -- so it leaves evidence.
        if tty.read(1) and ANSWERED:
            with open(ANSWERED, "w") as handle:
                handle.write("answered")
        return 255

    tty.write(b"dut@10.0.0.9's password: ")
    line = b""
    while not line.endswith(b"\n"):
        chunk = tty.read(1)
        if not chunk:
            return 255
        line += chunk
    supplied = line.strip().decode()

    if MODE == "echo":
        sys.stderr.write(f"remote said: {supplied}\n")
        sys.stderr.flush()
        return 255
    if MODE == "denied" or supplied != EXPECTED:
        sys.stderr.write(DENIAL + "\n")
        sys.stderr.flush()
        return 255

    # Authenticated. Become the remote command, exactly as ssh does -- which
    # makes the caller's own command string, and its `exec sh`, the thing under
    # test rather than a re-implementation of them.
    os.execvp("/bin/sh", ["sh", "-c", sys.argv[-1]])
    return 127  # unreachable


if __name__ == "__main__":
    sys.exit(main())
