import { describe, expect, it } from "vitest";

import { humanizeApiError } from "./rest";

/**
 * The busy-port message, and why it is worth a test of its own.
 *
 * pyserial's `Resource busy` is a true statement about the wrong device node.
 * One USB adapter presents two -- `/dev/cu.PL2303G-…` and `/dev/tty.PL2303G-…`
 * -- and a `minicom` holds the tty one, which blocks the cu one while leaving
 * the obvious `lsof /dev/cu.*` completely empty. An operator who runs that
 * check, sees nothing and reads the errno concludes the app is flaky. That
 * happened on this bench on 2026-09-04.
 *
 * So the copy has to name the tty node. A test asserting merely that the
 * message changed would pass on any rewrite that dropped it, which is the one
 * thing this message exists to say.
 */

const PYSERIAL_BUSY =
  '{"detail":"[Errno 16] could not open port /dev/cu.PL2303G-USBtoUART110:' +
  " [Errno 16] Resource busy: '/dev/cu.PL2303G-USBtoUART110'\"}";

describe("humanizeApiError on a busy serial port", () => {
  it("says another process holds the cable, and names the tty node", () => {
    const message = humanizeApiError(new Error(PYSERIAL_BUSY));
    expect(message).toContain("another process");
    expect(message).toContain("/dev/tty.");
    expect(message).not.toContain("Errno");
  });

  it("leaves an unrelated backend error alone", () => {
    expect(humanizeApiError(new Error('{"detail":"Unknown DUT: mesh1"}'))).toBe(
      "Unknown DUT: mesh1",
    );
  });
});
