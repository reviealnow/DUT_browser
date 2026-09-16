import { describe, expect, it } from "vitest";

import { dutPresence } from "./dutPresence";

/**
 * The distinction the old labels could not make.
 *
 * `idle` means "nothing has arrived for ten seconds" and was labelled "No DUT",
 * so a DUT whose console was held open and whose shell was simply sitting at a
 * prompt read as absent. Quiet and absent are different states and the registry
 * knows which is which.
 */
describe("what a DUT's pill says", () => {
  it("does not call a held console absent", () => {
    expect(dutPresence("idle", true).label).toBe("Connected");
  });

  it("still says No DUT when nothing is open", () => {
    expect(dutPresence("idle", false).label).toBe("No DUT");
  });

  it("leads with the stream when data is arriving, open or not", () => {
    // Streaming is the stronger statement: bytes are landing right now.
    expect(dutPresence("streaming", true).label).toBe("Streaming");
    expect(dutPresence("streaming", false).label).toBe("Streaming");
  });

  it("says Offline over everything, because nothing else could be known", () => {
    // The link this page learns every other fact through is down; a console
    // that was open a moment ago cannot be reported as open now.
    expect(dutPresence("offline", true).label).toBe("Offline");
  });

  it("gives the dot to the streaming state alone", () => {
    // A resting dot beside "Connected" is what raised the question in the
    // first place. Present only where it moves.
    expect(dutPresence("streaming", false).live).toBe(true);
    expect(dutPresence("idle", true).live).toBe(false);
    expect(dutPresence("idle", false).live).toBe(false);
    expect(dutPresence("offline", false).live).toBe(false);
  });
});
