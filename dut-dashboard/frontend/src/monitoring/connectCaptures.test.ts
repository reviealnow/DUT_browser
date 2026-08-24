// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The order of the connect batch, which is not a style preference.
 *
 * `capture_command` returns when its sentinel arrives *or its timeout expires*,
 * and the DUT keeps transmitting either way. A site survey on an AP with ~29
 * VAPs leaves tens of thousands of `iw scan` lines still draining at 115200
 * baud, so a capture started right after it has its whole window filled by that
 * backlog and reads nothing of its own — measured on an AP6_840E:
 * clients+capability returned 0 VAPs in 17.7s after a survey, and 29 VAPs in
 * 5.3s on an idle line.
 *
 * For the mesh probe that failure is worse than slow, because its empty answer
 * is not blank — it is "could not tell" on a device that is perfectly healthy
 * and meshed. Hence this test: the probe runs BEFORE the survey, and reading the
 * source is exactly how a reordering would get through review unnoticed.
 */

const order: string[] = [];
const captureDutContext = vi.fn(async () => { order.push("context"); });
const probeMesh = vi.fn(async () => { order.push("probe"); return {} as never; });
const getChannelRecommendation = vi.fn(async () => {
  order.push("survey");
  return { bands: [] } as never;
});

vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  captureDutContext: () => captureDutContext(),
  probeMesh: () => probeMesh(),
  getChannelRecommendation: () => getChannelRecommendation(),
}));

const { runConnectCaptures } = await import("./siteSurveyStore");

beforeEach(() => {
  order.length = 0;
  captureDutContext.mockClear();
  probeMesh.mockClear();
  getChannelRecommendation.mockClear();
});

describe("the connect capture batch", () => {
  it("probes for mesh before the survey floods the line", async () => {
    await runConnectCaptures("default");
    expect(order).toEqual(["context", "probe", "survey"]);
  });

  it("runs them in sequence, never concurrently", async () => {
    // They share one serial gate, so firing them together only makes them queue
    // behind each other — and a queued capture can time out.
    let inFlight = 0;
    let overlapped = false;
    const track = async (name: string) => {
      inFlight += 1;
      if (inFlight > 1) overlapped = true;
      await Promise.resolve();
      order.push(name);
      inFlight -= 1;
    };
    captureDutContext.mockImplementation(() => track("context"));
    probeMesh.mockImplementation(() => track("probe") as never);
    getChannelRecommendation.mockImplementation(() => track("survey") as never);

    await runConnectCaptures("default");
    expect(overlapped).toBe(false);
  });

  it("a failed probe does not fail the connect, and does not skip the survey", async () => {
    probeMesh.mockRejectedValueOnce(new Error("Serial capture is busy"));
    await expect(runConnectCaptures("default")).resolves.toBeUndefined();
    expect(getChannelRecommendation).toHaveBeenCalled();
  });

  it("a failed context capture still lets the probe run", async () => {
    captureDutContext.mockRejectedValueOnce(new Error("no serial"));
    await runConnectCaptures("default");
    expect(order).toEqual(["probe", "survey"]);
  });
});
