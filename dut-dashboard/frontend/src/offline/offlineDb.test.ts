import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { loadOfflineDuts, OfflineDutRecord, removeOfflineDut, saveOfflineDut } from "./offlineDb";

/**
 * How this module answers when the browser's storage does not cooperate.
 *
 * Two findings are pinned here, and both are about a promise that never
 * settles: an open whose `blocked` event nobody listens for leaves the section
 * on "Opening saved logs…" for ever, and a delete whose failure is not
 * reported lets the screen drop a record that is still in storage.
 *
 * `offlineDb.ts` calls the unqualified global `indexedDB`, so the fake below is
 * all the browser these tests need — no package, no DOM. It models only what
 * these three functions use: an open request, and one request per store
 * operation, each settled by hand so a test can choose the moment and the
 * outcome.
 */

type Handler = (() => void) | null;

/** A request the test settles itself, standing in for an `IDBRequest`. */
class FakeRequest<T> {
  result: T | undefined;
  error: Error | null = null;
  onsuccess: Handler = null;
  onerror: Handler = null;
  onblocked: Handler = null;
  onupgradeneeded: Handler = null;

  succeed(result?: T) {
    this.result = result;
    this.onsuccess?.();
  }

  fail(error: Error) {
    this.error = error;
    this.onerror?.();
  }
}

type StoreCall = { op: "getAll" | "put" | "delete"; store: string; mode: string; arg?: unknown };

class FakeDatabase {
  readonly created: string[] = [];
  readonly calls: StoreCall[] = [];
  readonly requests: FakeRequest<unknown>[] = [];
  readonly objectStoreNames = { contains: (name: string) => this.created.includes(name) };

  createObjectStore(name: string) {
    this.created.push(name);
  }

  transaction(store: string, mode = "readonly") {
    const request = (op: StoreCall["op"], arg?: unknown) => {
      this.calls.push({ op, store, mode, arg });
      const pending = new FakeRequest<unknown>();
      this.requests.push(pending);
      return pending;
    };
    return {
      objectStore: () => ({
        getAll: () => request("getAll"),
        put: (value: unknown) => request("put", value),
        delete: (key: unknown) => request("delete", key),
      }),
    };
  }

  /** The store request the call under test is waiting on. */
  pending() {
    return this.requests[this.requests.length - 1];
  }
}

const opens: Array<{ name: string; version: number; request: FakeRequest<FakeDatabase> }> = [];

beforeEach(() => {
  opens.length = 0;
  const fake = {
    open(name: string, version: number) {
      const request = new FakeRequest<FakeDatabase>();
      opens.push({ name, version, request });
      return request;
    },
  };
  Reflect.set(globalThis, "indexedDB", fake);
});

// Node has no `indexedDB`, so deleting it restores exactly what was here.
afterEach(() => {
  Reflect.deleteProperty(globalThis, "indexedDB");
});

/** The open request the call under test is waiting on. */
function pendingOpen() {
  return opens[opens.length - 1].request;
}

/** Lets every microtask the call under test is waiting on run. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Opens the database for a call already in flight, and hands it back once that
 * call has got as far as its store request — the call resumes on a microtask,
 * so the request does not exist the instant the open succeeds.
 */
async function openSucceeds() {
  const database = new FakeDatabase();
  pendingOpen().succeed(database);
  await flush();
  return database;
}

/**
 * What a promise did, shortly — including "nothing", which is the outcome
 * both of these findings are about. Waiting on a hung promise with plain
 * `rejects` would fail too, but only by timing out, and would report a slow
 * test rather than a section that never stops loading.
 */
async function outcome(promise: Promise<unknown>): Promise<string> {
  const pending = new Promise<string>((resolve) => setTimeout(() => resolve("pending"), 20));
  return Promise.race([
    promise.then(() => "resolved", (error: Error) => `rejected: ${error.message}`),
    pending,
  ]);
}

const RECORD: OfflineDutRecord = {
  id: "dut-1",
  name: "capture",
  sourceFile: "capture.log",
  createdAt: 10,
  rows: [],
  missing: 0,
};

/** Every way into this module, so no entry point can hang on its own. */
const ENTRY_POINTS: Array<[string, () => Promise<unknown>]> = [
  ["loadOfflineDuts", () => loadOfflineDuts()],
  ["saveOfflineDut", () => saveOfflineDut(RECORD)],
  ["removeOfflineDut", () => removeOfflineDut("dut-1")],
];

describe("an open the browser will not complete", () => {
  for (const [name, call] of ENTRY_POINTS) {
    it(`${name} rejects, rather than waiting for ever, when an upgrade is blocked`, async () => {
      // `blocked` fires when another tab still holds an older version. Nothing
      // upgrades today, so this cannot happen yet — which is exactly why it
      // needs a test: the version bump that makes it possible will be made by
      // someone who never sees this promise fail to settle.
      const promise = call();
      pendingOpen().onblocked?.();

      expect(await outcome(promise)).toBe(
        "rejected: Another tab has this browser's saved logs open. Close it and reload.",
      );
    });

    it(`${name} rejects when the database cannot be opened at all`, async () => {
      const promise = call();
      pendingOpen().fail(new Error("storage is disabled in this browser"));

      expect(await outcome(promise)).toBe("rejected: storage is disabled in this browser");
    });
  }

  it("creates the store the first time the database is opened", async () => {
    const promise = loadOfflineDuts();
    const database = new FakeDatabase();
    pendingOpen().result = database;
    pendingOpen().onupgradeneeded?.();
    pendingOpen().succeed(database);
    await flush();
    database.pending().succeed([]);

    await promise;
    expect(database.created).toEqual(["duts"]);
  });
});

describe("a delete that the browser refuses", () => {
  it("rejects, so the section can keep the record on screen and say so", async () => {
    // The other half of this finding — that the row stays visible and the
    // notice appears — lives in `OfflineAnalyzerSection`'s catch and needs a
    // rendered component to observe. It is NOT covered here. What is covered is
    // the half it depends on: a refused delete has to reach that catch at all.
    const promise = removeOfflineDut("dut-1");
    const database = await openSucceeds();
    database.pending().fail(new Error("QuotaExceededError"));

    expect(await outcome(promise)).toBe("rejected: QuotaExceededError");
  });

  it("resolves when the browser accepts it, and deletes that one record", async () => {
    const promise = removeOfflineDut("dut-1");
    const database = await openSucceeds();
    database.pending().succeed();

    expect(await outcome(promise)).toBe("resolved");
    expect(database.calls).toEqual([
      { op: "delete", store: "duts", mode: "readwrite", arg: "dut-1" },
    ]);
  });
});

describe("reading and writing the saved DUTs", () => {
  it("lists them oldest first, whatever order the store returns", async () => {
    const promise = loadOfflineDuts();
    const database = await openSucceeds();
    database.pending().succeed([
      { ...RECORD, id: "newer", createdAt: 20 },
      { ...RECORD, id: "older", createdAt: 10 },
    ]);

    expect((await promise).map((dut) => dut.id)).toEqual(["older", "newer"]);
  });

  it("reports a write that failed", async () => {
    const promise = saveOfflineDut(RECORD);
    const database = await openSucceeds();
    database.pending().fail(new Error("QuotaExceededError"));

    expect(await outcome(promise)).toBe("rejected: QuotaExceededError");
    expect(database.calls).toEqual([
      { op: "put", store: "duts", mode: "readwrite", arg: RECORD },
    ]);
  });
});
