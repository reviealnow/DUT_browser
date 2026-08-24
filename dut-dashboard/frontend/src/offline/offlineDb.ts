import type { LogRow } from "./logParser";

export type OfflineDutRecord = {
  id: string;
  name: string;
  sourceFile: string;
  createdAt: number;
  rows: LogRow[];
  missing: number;
};

const DB_NAME = "dut-dashboard-offline-analyzer";
const STORE_NAME = "duts";

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    // `blocked` fires when an open would upgrade the version while another tab
    // still holds the old one. It cannot fire today — the version is pinned at
    // 1 and nothing upgrades — so this is not a bug being fixed but a trap being
    // disarmed: without a handler the promise would neither resolve nor reject,
    // and the section would sit on "Opening saved logs…" for ever, the first
    // time somebody bumps the version.
    request.onblocked = () =>
      reject(new Error("Another tab has this browser's saved logs open. Close it and reload."));
  });
}

export async function loadOfflineDuts(): Promise<OfflineDutRecord[]> {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const request = database.transaction(STORE_NAME).objectStore(STORE_NAME).getAll();
    request.onsuccess = () => resolve((request.result as OfflineDutRecord[]).sort((a, b) => a.createdAt - b.createdAt));
    request.onerror = () => reject(request.error);
  });
}

export async function saveOfflineDut(dut: OfflineDutRecord): Promise<void> {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const request = database.transaction(STORE_NAME, "readwrite").objectStore(STORE_NAME).put(dut);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}

export async function removeOfflineDut(id: string): Promise<void> {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const request = database.transaction(STORE_NAME, "readwrite").objectStore(STORE_NAME).delete(id);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}
