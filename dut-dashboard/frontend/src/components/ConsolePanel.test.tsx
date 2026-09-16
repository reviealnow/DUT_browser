// @vitest-environment jsdom
import { fireEvent, render, RenderResult } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ConsolePanel from "./ConsolePanel";

/**
 * How the console is put into the DOM, which is not a cosmetic question.
 *
 * It used to be `lines.join("\n")` in one text node, so every batch rebuilt and
 * re-laid out the lot -- 6.39 ms an update on 1000 lines of real sysMon output,
 * measured in Chrome. Under a site survey that is most of the main thread and
 * the page stops answering the mouse.
 *
 * Per line with a stable key it is 0.52 ms, because React touches only what
 * arrived or left. The key is the whole trick, and the obvious key is the wrong
 * one: the window slides, so `index` names a different line after every batch
 * and React rewrites all thousand text nodes -- 7.44 ms, WORSE than the single
 * node it replaced.
 *
 * A test that only counted elements would pass on that mistake. So the test
 * below checks DOM node IDENTITY across a slide: the surviving lines must be
 * the same elements, not equal ones.
 *
 * The other trap is the faster shape. Block elements per line are quicker again
 * (0.50 ms) and they quietly break the console as something you copy out of: an
 * empty <div> contributes nothing to a selection, so every blank line the DUT
 * printed disappears from what gets pasted into a bug report. Hence the
 * serialisation test -- it is the one that goes red on that trade.
 */

function panel(lines: string[], linesStartSeq: number) {
  return (
    <ConsolePanel
      lines={lines}
      linesStartSeq={linesStartSeq}
      onSend={async () => {}}
      onDownloadLog={() => {}}
      canDownloadLog={false}
    />
  );
}

/** The scrollable box: the only element in here that scrolls its own content. */
function consoleBox(container: HTMLElement): HTMLElement {
  const box = Array.from(container.querySelectorAll("div")).find(
    (el) => el.style.overflowY === "auto" && el.style.whiteSpace === "pre-wrap",
  );
  if (!box) {
    throw new Error("console box not found");
  }
  return box;
}

describe("the console's line rendering", () => {
  it("gives every line its own element", () => {
    const { container } = render(panel(["first", "second", "third"], 0));
    const box = consoleBox(container);

    expect(Array.from(box.children).map((el) => el.textContent)).toEqual([
      "first\n",
      "second\n",
      "third\n",
    ]);
  });

  it("reuses the surviving elements when the window slides", () => {
    /* The assertion that catches an index key. Two lines arrive, two fall off
       the top; "c" and "d" are the same lines they were, so they must be the
       same nodes. With key={index} they are equal-but-rewritten and this fails. */
    const { container, rerender } = render(panel(["a", "b", "c", "d"], 0));
    const before = Array.from(consoleBox(container).children);

    rerender(panel(["c", "d", "e", "f"], 2));
    const after = Array.from(consoleBox(container).children);

    expect(after[0]).toBe(before[2]); // "c"
    expect(after[1]).toBe(before[3]); // "d"
    expect(after.map((el) => el.textContent)).toEqual(["c\n", "d\n", "e\n", "f\n"]);
  });

  it("serialises exactly like the joined text node it replaced", () => {
    /* The blank lines are the point. The DUT's output is full of them, and the
       faster shape -- one block element per line -- drops every one of them
       from a selection: an empty <div> serialises to nothing, so an operator
       pasting the console into a bug report loses gaps that were really there.
       Spans carrying their own newline serialise like the original did. */
    const lines = ["above", "", "", "  indented", "below"];
    const { container } = render(panel(lines, 0));

    expect(consoleBox(container).textContent).toBe(lines.join("\n") + "\n");
  });

  it("does not lose leading whitespace", () => {
    /* sysMon indents; `pre-wrap` on the box is what preserves it, and moving
       from one text node to many is exactly the change that could drop it. */
    const { container } = render(panel(["    indented"], 0));
    expect(consoleBox(container).children[0].textContent).toBe("    indented\n");
  });
});

describe("waking a console that has said nothing", () => {
  /**
   * A bare newline is the first thing anyone sends to a silent serial console:
   * a DUT at a shell prompt answers with its prompt, one waiting for a login
   * shows the login again, and one that is simply not connected stays silent --
   * which is the answer too.
   *
   * The command box will not send it. `sendCommand` drops an empty value on
   * purpose, so Send with nothing typed does nothing at all, and on the bench
   * the only ways left were to switch the whole console into terminal mode or
   * to invent a command to run.
   */
  function withSend() {
    const sent: string[] = [];
    const view = render(
      <ConsolePanel
        lines={[]}
        linesStartSeq={0}
        onSend={async (text) => {
          sent.push(text);
        }}
        onDownloadLog={() => {}}
        canDownloadLog={false}
      />,
    );
    return { sent, view };
  }

  const button = (view: RenderResult, label: string) =>
    [...view.container.querySelectorAll("button")].find((b) => b.textContent === label)!;

  it("sends exactly one newline, and nothing else", () => {
    const { sent, view } = withSend();
    fireEvent.click(button(view, "Send Enter"));
    expect(sent).toEqual(["\n"]);
  });

  it("leaves a half-typed command alone", () => {
    // The reason this is its own button rather than a meaning for an empty
    // Send: somebody mid-command still wants to see whether the far end is
    // awake, and losing what they typed to find out would be a poor trade.
    const { sent, view } = withSend();
    const input = view.container.querySelector("input")!;
    fireEvent.change(input, { target: { value: "cat /proc/cpuinfo" } });
    fireEvent.click(button(view, "Send Enter"));
    expect(sent).toEqual(["\n"]);
    expect(input.value).toBe("cat /proc/cpuinfo");
  });

  it("still refuses to send an empty command from the box itself", () => {
    // The guard that made the button necessary stays: Send on an empty box is
    // a misclick, not a request to poke the DUT.
    const { sent, view } = withSend();
    fireEvent.click(button(view, "Send"));
    expect(sent).toEqual([]);
  });
});
