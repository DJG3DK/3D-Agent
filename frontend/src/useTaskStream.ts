import { useEffect, useRef, useState } from "react";
import { AuthError, getMe, getTask, taskStreamUrl } from "./api";
import type { LogEntry, PendingApproval, PlanStep, ReviewGateResult, StreamEvent, TaskStatus } from "./types";

interface StreamState {
  log: LogEntry[];
  plan: PlanStep[];
  currentStepIndex: number;
  costSoFar: number;
  escalated: boolean;
  escalationReason: string | null;
  reviewGateResult: ReviewGateResult | null;
  pendingApproval: PendingApproval | null;
  committedSha: string | null;
  status: TaskStatus | "connecting";
  connected: boolean;
  hydrateError: string | null;
  // True when the Store says "running" but nothing is actually driving the
  // task (a backend restart mid-run — same condition resume_task's own
  // eligibility check already accepts). Only ever known from the REST
  // hydrate snapshot, not the WS stream — a dead task's WS endpoint still
  // accepts the connection and just sits there forever, so there's no
  // WS-side signal to detect this from.
  orphaned: boolean;
}

// audit H-16: the log renders unvirtualized, so beyond React.memo (which
// removes the O(N^2) regex cost) a very long autonomous run still piles up
// live DOM nodes. Cap retained entries to a generous ceiling; slicing keeps
// the surviving entries' object references intact, so ChatMessage's memo
// still short-circuits. cleanText-heavy history beyond this is dropped from
// the view (the full record lives server-side).
const MAX_LOG_ENTRIES = 3000;

const EMPTY_STATE: StreamState = {
  log: [],
  plan: [],
  currentStepIndex: 0,
  costSoFar: 0,
  escalated: false,
  escalationReason: null,
  reviewGateResult: null,
  pendingApproval: null,
  committedSha: null,
  status: "connecting",
  connected: false,
  hydrateError: null,
  orphaned: false,
};

const HYDRATE_MAX_RETRIES = 5;

/**
 * Auto-reconnects with backoff on an unexpected drop — a silent disconnect
 * that looks like the agent just stopped is worse than a visible retry. A
 * deliberate server-sent "closed" event (task actually finished) stops
 * reconnecting; anything else (network blip, server restart) retries.
 *
 * `generation` forces a fresh connection without treating it as a new task
 * (used after resuming an escalated task — the server-side WS handler
 * genuinely closes when a run finishes, by design, so picking the same task
 * back up needs a new connection, but should keep the existing log history
 * rather than wiping it like switching tasks does).
 *
 * The WS only carries live events from the moment it connects — it never
 * replays what already happened. `getTask` (a plain REST snapshot of the
 * current checkpointed state) fills that gap on every connect/reconnect, so
 * the UI reflects reality immediately regardless of when the viewer showed
 * up. This snapshot fetch retries with backoff and surfaces a visible error
 * via `hydrateError` if it keeps failing, rather than leaving the view
 * stuck on a blank/"connecting" state with no indication anything is wrong.
 */
export function useTaskStream(taskId: string | null, repo: string | null, generation: number = 0): StreamState {
  const [state, setState] = useState<StreamState>(EMPTY_STATE);
  const closedIntentionally = useRef(false);
  const prevTaskId = useRef<string | null>(null);

  useEffect(() => {
    if (!taskId || !repo) return;
    closedIntentionally.current = false;
    const isNewTask = prevTaskId.current !== taskId;
    prevTaskId.current = taskId;
    let cancelled = false;

    async function hydrate(): Promise<boolean> {
      for (let attempt = 0; attempt <= HYDRATE_MAX_RETRIES; attempt++) {
        if (cancelled) return false;
        try {
          const { meta, state: graphState, orphaned } = await getTask(taskId!, repo!);
          if (cancelled) return false;
          if (graphState) {
            setState((s) => ({
              ...s,
              // audit M-18: never SHRINK the log. work_node only persists
              // execution_log at the end of a pass, so a reconnect mid-pass
              // returns an older, shorter checkpointed list that would replace
              // live-streamed entries and make the log visibly shrink. Keep
              // whichever is longer. (The full fix -- open the socket first,
              // buffer, then hydrate + replay with a monotonic event id for
              // dedup -- is a larger backend change; this removes the concrete
              // shrink symptom safely.)
              log: (graphState.execution_log && graphState.execution_log.length >= s.log.length)
                ? graphState.execution_log
                : s.log,
              plan: graphState.plan ?? s.plan,
              currentStepIndex: graphState.current_step_index ?? s.currentStepIndex,
              costSoFar: graphState.cost_so_far ?? s.costSoFar,
              escalated: graphState.escalated ?? s.escalated,
              escalationReason: graphState.escalation_reason ?? s.escalationReason,
              reviewGateResult: graphState.review_gate_result ?? s.reviewGateResult,
              committedSha: graphState.committed_sha ?? s.committedSha,
              // Not `?? s.pendingApproval`: this field must be able to go
              // from a real object back to null on reconnect (the approval
              // was resolved while disconnected) -- `??` would treat that
              // legitimate null as "missing" and incorrectly keep showing a
              // stale approval card. The REST snapshot always includes this
              // key (outer_state.py's AgentState always has it), so it's
              // safe to take verbatim rather than fall back.
              pendingApproval: graphState.pending_approval,
              status: meta.status,
              hydrateError: null,
              orphaned,
            }));
          } else {
            setState((s) => ({ ...s, status: meta.status, hydrateError: null, orphaned }));
          }
          return true;
        } catch (err) {
          console.error(`getTask attempt ${attempt + 1}/${HYDRATE_MAX_RETRIES + 1} failed:`, err);
          if (attempt === HYDRATE_MAX_RETRIES) {
            setState((s) => ({
              ...s,
              hydrateError: "Couldn't load this task's current state after several attempts. Reload the page to retry.",
            }));
            return false;
          }
          await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 8000)));
        }
      }
      return false;
    }

    // audit H6: every reconnect path used to call connect() directly, so only
    // the FIRST connection ever fetched a snapshot. A network blip or a backend
    // restart therefore dropped whatever the socket missed while it was down --
    // permanently, since nothing re-read it afterwards. The graceful
    // closed-frame path re-hydrated; the three unclean paths did not, which is
    // exactly backwards: an unclean drop is when a gap is most likely.
    async function reconnect() {
      if (cancelled || closedIntentionally.current) return;
      await hydrate();
      if (cancelled || closedIntentionally.current) return;
      connect();
    }

    async function hydrateAndConnect() {
      if (isNewTask) {
        setState({ ...EMPTY_STATE, status: "connecting" });
      } else {
        setState((s) => ({ ...s, status: "connecting", connected: false, hydrateError: null }));
      }
      await hydrate();
      if (cancelled) return;
      connect();
    }

    let ws: WebSocket;
    let retryDelay = 1000;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let watchTimer: ReturnType<typeof setTimeout> | undefined;

    // A server "closed" event means this run ended -- it does not mean the
    // task is over forever. A task paused at awaiting_approval (or stopped
    // for a backend fix) sends "closed"; if it's later resumed from outside
    // this page (an operator API call), the page must not permanently stop
    // reconnecting -- the approval request and every subsequent status
    // change would otherwise be invisible until a manual refresh. After
    // "closed", keep polling the REST snapshot (status/approvals/log stay
    // current within seconds either way) and re-open the live stream the
    // moment the task is running again.
    // audit M-19: a "closed" event does not always mean the task can still
    // change. done/error are settled -- a resume from THIS page bumps generation
    // and re-runs the effect, so there is nothing to poll for. Only the states
    // that can transition WITHOUT this page's involvement (an external approval
    // or an orphaned task being resumed elsewhere) are worth watching. The old
    // code polled every 6s forever for done/error/stopped too, and kept doing
    // it while the user sat on Analytics or a hidden tab.
    const _TERMINAL_SETTLED = new Set(["done", "error"]);
    const _WATCH_MIN_MS = 6000;
    const _WATCH_MAX_MS = 60000;
    let watchDelay = _WATCH_MIN_MS;

    function watchForResumption() {
      clearTimeout(watchTimer);
      const tick = async () => {
        if (cancelled) {
          clearTimeout(watchTimer);
          return;
        }
        // Pause polling while the tab is hidden -- resume on the next tick once
        // it's visible again (don't advance backoff on a skipped tick).
        if (typeof document !== "undefined" && document.hidden) {
          watchTimer = setTimeout(tick, watchDelay);
          return;
        }
        try {
          const { meta, state: graphState, orphaned } = await getTask(taskId!, repo!);
          if (cancelled) return;
          setState((s) => ({
            ...s,
            status: meta.status,
            orphaned,
            costSoFar: graphState?.cost_so_far ?? s.costSoFar,
            committedSha: graphState?.committed_sha ?? s.committedSha,
            // audit M-18: never shrink (see hydrate's note).
            log: (graphState?.execution_log && graphState.execution_log.length >= s.log.length)
              ? graphState.execution_log
              : s.log,
            plan: graphState?.plan ?? s.plan,
            escalated: graphState?.escalated ?? s.escalated,
            escalationReason: graphState?.escalation_reason ?? s.escalationReason,
            reviewGateResult: graphState?.review_gate_result ?? s.reviewGateResult,
            pendingApproval: graphState ? graphState.pending_approval : s.pendingApproval,
          }));
          if (meta.status === "running") {
            clearTimeout(watchTimer);
            closedIntentionally.current = false;
            connect();
            return;
          }
          // Nothing left to watch for on a settled task -- stop the loop.
          if (_TERMINAL_SETTLED.has(meta.status)) {
            clearTimeout(watchTimer);
            return;
          }
        } catch {
          // Transient poll failure -- keep watching.
        }
        watchDelay = Math.min(watchDelay * 1.5, _WATCH_MAX_MS);
        watchTimer = setTimeout(tick, watchDelay);
      };
      watchDelay = _WATCH_MIN_MS;
      watchTimer = setTimeout(tick, watchDelay);
    }

    function connect() {
      ws = new WebSocket(taskStreamUrl(taskId!));
      // audit H-14: a rejected upgrade (expired/invalid session) fires onclose
      // WITHOUT ever firing onopen, and the old code just reconnected on the
      // same backoff forever against a server that will never accept it.
      let openedThisAttempt = false;

      ws.onopen = () => {
        openedThisAttempt = true;
        retryDelay = 1000;
        setState((s) => ({ ...s, connected: true }));
      };

      ws.onmessage = (ev) => {
        const event: StreamEvent = JSON.parse(ev.data);
        if (event.type === "ping") return; // server heartbeat, not content
        if (event.type === "closed") {
          closedIntentionally.current = true;
          watchForResumption();
          return;
        }
        setState((s) => ({
          log: event.execution_log
            ? (() => {
                const next = [...s.log, ...event.execution_log];
                return next.length > MAX_LOG_ENTRIES ? next.slice(-MAX_LOG_ENTRIES) : next;
              })()
            : s.log,
          plan: event.plan ?? s.plan,
          currentStepIndex: event.current_step_index ?? s.currentStepIndex,
          costSoFar: event.cost_so_far ?? s.costSoFar,
          committedSha: event.committed_sha ?? s.committedSha,
          escalated: event.escalated ?? s.escalated,
          escalationReason: event.escalation_reason ?? s.escalationReason,
          reviewGateResult: event.review_gate_result ?? s.reviewGateResult,
          // Same reasoning as the hydrate path above: `pending_approval` is
          // only included by server.py on a "work" node_update or a
          // "status" event, but is ALWAYS included (possibly as an explicit
          // null) whenever it is -- work_node's own return dict always has
          // this key. `"pending_approval" in event` distinguishes "this
          // event doesn't speak to approval state at all" (a todos/
          // log_entry custom event) from "the approval state is definitely
          // X now, even if X is null" -- `??` alone can't tell those apart
          // since it treats an explicit null the same as absent.
          pendingApproval: "pending_approval" in event ? (event.pending_approval ?? null) : s.pendingApproval,
          status: event.status ?? s.status,
          connected: true,
          hydrateError: null,
          // A live event is direct proof the task is being driven right
          // now, regardless of what the last hydrate snapshot said.
          orphaned: false,
        }));
      };

      ws.onclose = () => {
        // `cancelled` is per-effect-run; `closedIntentionally` is a ref shared
        // across runs, and checking only the ref produced DUPLICATED stream
        // output. On a generation bump (a resume), cleanup sets the ref true
        // and tears down the old socket -- but the new effect run has already
        // reset the same ref to false by the time the old socket's onclose
        // actually fires, so the OLD connection saw "not intentional" and
        // reconnected itself. Two live sockets for one task, both appending
        // to the same log, so every entry rendered twice. Checking `cancelled`
        // first is what distinguishes "this effect run is over, a newer one
        // owns the connection now" from "the server dropped us, reconnect".
        if (cancelled) return;
        setState((s) => ({ ...s, connected: false }));
        if (closedIntentionally.current) return;
        // audit H-14: if the socket closed without ever opening, re-validate the
        // session before scheduling another retry. A 401 from getMe fires the
        // central auth-failure handler (App clears the user -> login screen) and
        // we stop, instead of hammering a server that keeps rejecting the upgrade.
        if (!openedThisAttempt) {
          getMe()
            .then(() => {
              if (cancelled || closedIntentionally.current) return;
              retryTimer = setTimeout(reconnect, retryDelay);
              retryDelay = Math.min(retryDelay * 2, 15000);
            })
            .catch((err) => {
              // AuthError -> the central handler already logged the user out;
              // do NOT reschedule (there is nothing to reconnect to). Any other
              // error means the API is unreachable too, so back off and retry.
              if (err instanceof AuthError) return;
              if (cancelled || closedIntentionally.current) return;
              retryTimer = setTimeout(reconnect, retryDelay);
              retryDelay = Math.min(retryDelay * 2, 15000);
            });
          return;
        }
        retryTimer = setTimeout(reconnect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 15000);
      };
    }

    hydrateAndConnect();
    return () => {
      cancelled = true;
      closedIntentionally.current = true;
      clearTimeout(retryTimer);
      clearTimeout(watchTimer);
      ws?.close();
    };
  }, [taskId, repo, generation]);

  return state;
}
