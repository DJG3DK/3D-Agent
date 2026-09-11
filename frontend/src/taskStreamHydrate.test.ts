import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { mergeLog } from "./useTaskStream";
import type { LogEntry } from "./types";

/* Socket-first hydrate, and a log that cannot shrink or double.
 *
 * The stream had two sources for one log -- the live socket and the REST
 * snapshot -- and no way to tell whether an entry from one was the same entry
 * from the other, so both ends kept "whichever list is longer". That is a
 * proxy for freshness and it is wrong both ways. Entries now carry a
 * content-derived id from the server and events carry a monotonic seq, which
 * is what lets the browser open its socket BEFORE hydrating and replay what
 * arrived meanwhile.
 */

const getTask = vi.fn();
const getMe = vi.fn();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getTask: (...a: unknown[]) => getTask(...a),
    getMe: (...a: unknown[]) => getMe(...a),
    taskStreamUrl: () => "ws://test/task",
  };
});

const sockets: FakeSocket[] = [];

class FakeSocket {
  static CLOSED = 3;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  readyState = 1;
  constructor() {
    sockets.push(this);
    queueMicrotask(() => this.onopen?.());
  }
  close() {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.();
  }
  send(data: string) {
    this.onmessage?.({ data });
  }
}

function entry(id: string, summary: string): LogEntry {
  return {
    id, summary, node: "work", step_id: null, detail: "", cost_usd: 0,
    timestamp: `2026-09-11T10:00:0${id}Z`,
  } as LogEntry & { id: string };
}

function snapshot(log: LogEntry[], seq = 0, status = "running") {
  return {
    meta: { status, task_id: "t1", repo: "proj" },
    state: { execution_log: log, plan: [], cost_so_far: 0, pending_approval: null },
    orphaned: false,
    seq,
  };
}

beforeEach(() => {
  sockets.length = 0;
  getTask.mockReset();
  getMe.mockReset();
  getMe.mockResolvedValue({});
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function mount() {
  const { useTaskStream } = await import("./useTaskStream");
  const hook = renderHook(() => useTaskStream("t1", "proj"));
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  return hook;
}

describe("mergeLog", () => {
  it("keeps snapshot order and appends only what the socket added", () => {
    const merged = mergeLog([entry("1", "a"), entry("2", "b")], [entry("2", "b"), entry("3", "c")]);
    expect(merged.map((e) => e.summary)).toEqual(["a", "b", "c"]);
  });

  it("never shrinks when the snapshot is older and shorter", () => {
    // work_node only persists execution_log when a pass RETURNS, so a
    // reconnect mid-pass gets a shorter list than the browser already holds.
    const live = [1, 2, 3, 4, 5].map((n) => entry(String(n), `live ${n}`));
    const stale = live.slice(0, 2);
    expect(mergeLog(stale, live).map((e) => e.summary)).toEqual(live.map((e) => e.summary));
  });

  it("keeps history the browser never saw", () => {
    const before = [1, 2, 3].map((n) => entry(String(n), `old ${n}`));
    const since = [4, 5].map((n) => entry(String(n), `new ${n}`));
    expect(mergeLog(before, since).map((e) => e.summary))
      .toEqual(["old 1", "old 2", "old 3", "new 4", "new 5"]);
  });

  it("is idempotent, so replaying a frame is harmless", () => {
    const entries = [entry("1", "a"), entry("2", "b")];
    expect(mergeLog(mergeLog(entries, entries), entries)).toHaveLength(2);
  });

  it("falls back to content when an older server sends no id", () => {
    const noId: LogEntry = { timestamp: "t", node: "work", step_id: null, summary: "a", detail: "", cost_usd: 0 };
    expect(mergeLog([noId], [{ ...noId }])).toHaveLength(1);
  });
});

describe("socket-first hydrate", () => {
  it("opens the socket before fetching the snapshot", async () => {
    let socketsAtFetch = -1;
    getTask.mockImplementation(async () => {
      socketsAtFetch = sockets.length;
      return snapshot([entry("1", "from snapshot")]);
    });
    await mount();
    expect(socketsAtFetch).toBe(1);
  });

  it("keeps an event that arrives during the hydrate, and applies it after", async () => {
    // The window this closes: an event published between the snapshot being
    // taken and the socket being open used to reach nobody.
    let release: (v: unknown) => void = () => {};
    getTask.mockImplementation(() => new Promise((r) => {
      release = () => r(snapshot([entry("1", "from snapshot")], 1));
    }));
    const hook = await mount();
    const ws = sockets[0];

    act(() => ws.send(JSON.stringify({ seq: 2, execution_log: [entry("2", "during hydrate")] })));
    expect(hook.result.current.log).toHaveLength(0);   // buffered, not applied yet

    await act(async () => { release(null); await vi.advanceTimersByTimeAsync(10); });

    expect(hook.result.current.log.map((e) => e.summary)).toEqual(["from snapshot", "during hydrate"]);
  });

  it("drops a buffered event the snapshot already contains", async () => {
    getTask.mockResolvedValue(snapshot([entry("1", "a"), entry("2", "b")], 5));
    const hook = await mount();
    const ws = sockets[0];

    // seq 5 and below is already in the snapshot; 6 is new.
    act(() => ws.send(JSON.stringify({ seq: 4, cost_so_far: 99 })));
    act(() => ws.send(JSON.stringify({ seq: 6, cost_so_far: 7 })));

    expect(hook.result.current.costSoFar).toBe(7);
  });

  it("does not double entries when a reconnect re-sends them", async () => {
    getTask.mockResolvedValue(snapshot([entry("1", "a")], 1));
    const hook = await mount();
    const ws = sockets[0];

    act(() => ws.send(JSON.stringify({ seq: 2, execution_log: [entry("2", "b")] })));
    expect(hook.result.current.log).toHaveLength(2);

    // A reconnect: the snapshot now contains both, and the socket re-sends b.
    getTask.mockResolvedValue(snapshot([entry("1", "a"), entry("2", "b")], 2));
    act(() => ws.close());
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });

    expect(hook.result.current.log.map((e) => e.summary)).toEqual(["a", "b"]);
  });
});

describe("a stalled agent on a healthy socket", () => {
  it("reports how long it has been quiet, and pings do not count as progress", async () => {
    getTask.mockResolvedValue(snapshot([], 0));
    const hook = await mount();
    const ws = sockets[0];

    // Five minutes of nothing but heartbeats: the socket is fine, the agent
    // is not moving. The liveness watchdog cannot see this.
    for (let i = 0; i < 15; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      act(() => ws.send(JSON.stringify({ type: "ping" })));
    }

    expect(ws.closed).toBe(false);                       // a ping is proof of a live socket
    expect(hook.result.current.idleSeconds).toBeGreaterThanOrEqual(290);
  });

  it("resets the moment real work arrives", async () => {
    getTask.mockResolvedValue(snapshot([], 0));
    const hook = await mount();
    const ws = sockets[0];

    // Pings throughout, or the liveness watchdog would (correctly) recycle
    // the socket at 70s and the fresh hydrate would reset the counter -- the
    // two mechanisms are for different failures and must not be confused.
    for (let i = 0; i < 9; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      act(() => ws.send(JSON.stringify({ type: "ping" })));
    }
    expect(hook.result.current.idleSeconds).toBeGreaterThan(150);
    expect(ws.closed).toBe(false);

    act(() => ws.send(JSON.stringify({ seq: 1, execution_log: [entry("1", "ran the tests")] })));
    expect(hook.result.current.idleSeconds).toBe(0);
  });

  it("stays at zero for a task that is not running", async () => {
    getTask.mockResolvedValue(snapshot([], 0, "awaiting_merge"));
    const hook = await mount();

    await act(async () => { await vi.advanceTimersByTimeAsync(300_000); });

    expect(hook.result.current.idleSeconds).toBe(0);
  });
});

describe("residuals found on review", () => {
  it("adopts a snapshot seq that went BACKWARDS after a server restart", async () => {
    // The server's counter dies with its process. A browser holding 500 from
    // the old one would drop every frame from the new one until it climbed
    // past 500 -- silently, on exactly the reconnect-after-restart path.
    getTask.mockResolvedValue(snapshot([], 500));
    const hook = await mount();
    const ws = sockets[0];
    act(() => ws.send(JSON.stringify({ seq: 501, cost_so_far: 1 })));
    expect(hook.result.current.costSoFar).toBe(1);

    // The backend restarts: the socket drops and the new process starts at 1.
    getTask.mockResolvedValue(snapshot([], 0));
    act(() => ws.close());
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });

    const fresh = sockets[sockets.length - 1];
    act(() => fresh.send(JSON.stringify({ seq: 1, cost_so_far: 42 })));
    expect(hook.result.current.costSoFar).toBe(42);
  });

  it("re-hydrates rather than bare-connecting when a parked task starts running elsewhere", async () => {
    // An operator approving from another device, or an orphan resumed
    // elsewhere. This path called connect() alone, which left the
    // hydrate-window hole open and skipped the seq re-adoption.
    getTask.mockResolvedValue(snapshot([entry("1", "a")], 3, "awaiting_approval"));
    const hook = await mount();
    const ws = sockets[0];
    const socketsBefore = sockets.length;

    act(() => ws.send(JSON.stringify({ type: "closed" })));   // server parks the run
    getTask.mockResolvedValue(snapshot([entry("1", "a"), entry("2", "b")], 4, "running"));

    const callsBefore = getTask.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });

    expect(sockets.length).toBeGreaterThan(socketsBefore);        // reconnected
    expect(getTask.mock.calls.length).toBeGreaterThan(callsBefore + 1);  // polled AND hydrated
    expect(hook.result.current.log.map((e) => e.summary)).toEqual(["a", "b"]);
  });

  it("merges rather than replaces while the socket is closed", async () => {
    getTask.mockResolvedValue(snapshot([entry("1", "a"), entry("2", "b")], 2));
    const hook = await mount();
    const ws = sockets[0];
    act(() => ws.send(JSON.stringify({ seq: 3, execution_log: [entry("3", "c")] })));
    expect(hook.result.current.log).toHaveLength(3);

    // Parked, and the REST poll returns the shorter checkpointed list.
    act(() => ws.send(JSON.stringify({ type: "closed" })));
    getTask.mockResolvedValue(snapshot([entry("1", "a")], 1, "awaiting_approval"));
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });

    expect(hook.result.current.log.map((e) => e.summary)).toEqual(["a", "b", "c"]);
  });

  it("does not confuse two unstamped entries that differ only in step_id or late detail", () => {
    // The fallback id ran against an older server's payloads and omitted
    // step_id, cost and everything past 120 characters of detail, so two
    // distinct entries could collide and the second be dropped.
    const base = { timestamp: "t", node: "work" as const, summary: "s", detail: "x".repeat(400), cost_usd: 0 };
    const a: LogEntry = { ...base, step_id: "step-1" };
    const b: LogEntry = { ...base, step_id: "step-2" };
    const c: LogEntry = { ...base, step_id: "step-1", detail: "x".repeat(399) + "y" };
    expect(mergeLog([a], [b, c])).toHaveLength(3);
  });
});
