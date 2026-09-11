import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

/* A build task whose socket is silently dead.
 *
 * Planning chat has had this watchdog since 2026-08-31; build tasks did not,
 * which is the longer-running of the two. A half-open TCP (laptop sleep, NAT
 * idle-kill, a proxy dropping without a FIN) leaves the browser holding a
 * socket that never fires another event, so the task's later steps, its
 * approval requests and its final status all go to a socket nobody is
 * listening on. The page keeps rendering "running" on a step that finished
 * long ago -- indistinguishable from a genuinely stuck agent.
 *
 * Recovery is close(): onclose re-hydrates, and hydration reads status from
 * the server, which is the authority.
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
}

function runningSnapshot() {
  return {
    meta: { status: "running", task_id: "t1", repo: "proj" },
    state: { execution_log: [], plan: [], cost_so_far: 0, pending_approval: null },
    orphaned: false,
  };
}

beforeEach(() => {
  sockets.length = 0;
  getTask.mockReset();
  getMe.mockReset();
  getMe.mockResolvedValue({});
  getTask.mockResolvedValue(runningSnapshot());
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function mountRunningTask() {
  const { useTaskStream } = await import("./useTaskStream");
  const hook = renderHook(() => useTaskStream("t1", "proj"));
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  return hook;
}

describe("task socket liveness", () => {
  it("gives up on a socket that has gone quiet past three server pings", async () => {
    const hook = await mountRunningTask();
    expect(hook.result.current.status).toBe("running");
    const ws = sockets[sockets.length - 1];
    expect(ws.closed).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(80_000); });

    expect(ws.closed).toBe(true);
  });

  it("leaves a socket alone while the server keeps pinging", async () => {
    const hook = await mountRunningTask();
    const ws = sockets[sockets.length - 1];

    for (let i = 0; i < 5; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      act(() => ws.onmessage?.({ data: JSON.stringify({ type: "ping" }) }));
    }

    expect(ws.closed).toBe(false);
    expect(hook.result.current.status).toBe("running");
  });

  it("counts a content frame as proof of life, not only a ping", async () => {
    await mountRunningTask();
    const ws = sockets[sockets.length - 1];

    for (let i = 0; i < 4; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      act(() => ws.onmessage?.({ data: JSON.stringify({ type: "node_update", cost_so_far: i }) }));
    }

    expect(ws.closed).toBe(false);
  });

  it("does not watch a task that is not running", async () => {
    getTask.mockResolvedValue({
      meta: { status: "awaiting_merge", task_id: "t1", repo: "proj" },
      state: { execution_log: [], plan: [], pending_approval: null },
      orphaned: false,
    });
    await mountRunningTask();
    const ws = sockets[sockets.length - 1];

    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });

    expect(ws.closed).toBe(false);
  });

  it("reconnects after the watchdog fires, so the view recovers on its own", async () => {
    await mountRunningTask();
    const first = sockets.length;

    await act(async () => { await vi.advanceTimersByTimeAsync(80_000); });
    // onclose schedules a backoff retry; let it run.
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });

    expect(sockets.length).toBeGreaterThan(first);
    expect(getTask).toHaveBeenCalled();      // re-hydrated from the server
  });
});
