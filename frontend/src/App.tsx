import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { createTask, getMe, listPlanningSessions, listRepos, listTasks, logout, setAuthFailureHandler, uploadFiles } from "./api";
import { ChangePasswordPage } from "./components/ChangePasswordPage";
import { LoginPage } from "./components/LoginPage";
import { ModelConfigPanel } from "./components/ModelConfigPanel";
import { ConsolidationStatusPanel } from "./components/ConsolidationStatusPanel";
import { MobileNav } from "./components/MobileNav";
import { Icon } from "./components/Icon";
import { NewTaskPanel } from "./components/NewTaskPanel";
import { PlanningView } from "./components/PlanningView";
import { SettingsPage } from "./components/SettingsPage";
import { SetupTotpPage } from "./components/SetupTotpPage";
import { Sidebar } from "./components/Sidebar";
import { TaskView } from "./components/TaskView";
import { UsersPanel } from "./components/UsersPanel";
import type { CurrentUser, PlanningSessionMeta, TaskMeta } from "./types";
// audit M-23: recharts (~the bulk of the bundle) now ships in its own chunk,
// fetched only when an admin opens Analytics.
const AnalyticsView = lazy(() =>
  import("./components/AnalyticsView").then((m) => ({ default: m.AnalyticsView })),
);
import { useTaskStream } from "./useTaskStream";
import "./App.css";

type View = "new-task" | "task" | "analytics" | "models" | "planning" | "users" | "settings";

function AuthenticatedApp({ user, onLogout, onUserChanged }: { user: CurrentUser; onLogout: () => void; onUserChanged: (u: CurrentUser) => void }) {
  const [repos, setRepos] = useState<string[]>([]);
  const [tasks, setTasks] = useState<TaskMeta[]>([]);
  const [selected, setSelected] = useState<TaskMeta | null>(null);
  const [planningSessions, setPlanningSessions] = useState<PlanningSessionMeta[]>([]);
  const [selectedPlanningSession, setSelectedPlanningSession] = useState<PlanningSessionMeta | null>(null);
  // Plan-first (2026-08-28): the app lands on Planning -- a fresh session
  // panel -- not the raw task composer. Planning chat always happens first;
  // Build Now is how tasks get made.
  const [view, setView] = useState<View>("planning");
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // Bumping this forces useTaskStream to open a fresh WS connection after a
  // resume, without wiping the log history the way switching to a different
  // task does — see the hook's own doc comment for why a resume needs this
  // (the server-side connection genuinely closes when a run finishes).
  const [generation, setGeneration] = useState(0);
  // Called unconditionally (not just while view === "task") so switching to
  // Analytics/Planning/etc. and back never drops the WS connection or the
  // log entries already streamed in -- see TaskView.tsx's own comment on why
  // that used to happen.
  const taskStream = useTaskStream(selected?.task_id ?? null, selected?.repo ?? null, generation);
  // Mobile only (<=768px, see App.css): which pane is visible. On desktop
  // both panes always render side by side and this class has no effect.
  const [mobilePane, setMobilePane] = useState<"list" | "main">("list");

  const refreshTasks = useCallback(async () => {
    try {
      setTasks(await listTasks());
    } catch {
      // Backend not reachable yet — sidebar just stays on its last known list.
    }
  }, []);

  const refreshPlanningSessions = useCallback(async () => {
    try {
      setPlanningSessions(await listPlanningSessions());
    } catch {
      // Same tolerance as refreshTasks -- sidebar just stays on its last known list.
    }
  }, []);

  useEffect(() => {
    // Repos come back already scoped to this user's own access (see
    // GET /api/repos) -- a restricted account simply never sees a project
    // it can't touch, no separate frontend filtering needed anywhere below.
    listRepos().then(setRepos).catch(() => {});
    refreshTasks();
    refreshPlanningSessions();
    const interval = setInterval(() => {
      refreshTasks();
      refreshPlanningSessions();
    }, 8000); // catches status/cost/title changes for anything other than the selected item
    return () => clearInterval(interval);
  }, [refreshTasks, refreshPlanningSessions]);

  async function handleCreate(goal: string, repo: string, budgetUsd: number, files: File[]) {
    setSubmitting(true);
    setCreateError(null);
    try {
      const attachments = files.length ? await uploadFiles(repo, files) : undefined;
      const { task_id } = await createTask(goal, repo, budgetUsd, attachments);
      const meta: TaskMeta = { task_id, goal, repo, budget_usd: budgetUsd, status: "running", created_at: Date.now() / 1000 };
      setTasks((t) => [meta, ...t]);
      setSelected(meta);
      setView("task");
      setMobilePane("main");
    } catch (err) {
      // audit M-20: surface the failure instead of just flickering the button.
      // A 413 on a large attachment (or any create/upload error) now tells the
      // operator what happened rather than silently doing nothing.
      setCreateError(err instanceof Error ? err.message : "Failed to start the task. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const viewTitle =
    view === "task" ? "Task"
    : view === "analytics" ? "Analytics"
    : view === "models" ? "Models"
    : view === "planning" ? "Planning"
    : view === "users" ? "Users"
    : view === "settings" ? "Settings"
    : "New task";

  return (
    <div className={`app-shell ${mobilePane === "main" ? "show-main" : "show-list"}`}>
      <Sidebar
        tasks={tasks}
        selectedTaskId={selected?.task_id ?? null}
        planningSessions={planningSessions}
        selectedPlanningSessionId={selectedPlanningSession?.session_id ?? null}
        view={view}
        user={user}
        onSelect={(t) => {
          setSelected(t);
          setView("task");
          setMobilePane("main");
        }}
        onNewTask={() => {
          setView("new-task");
          setMobilePane("main");
        }}
        onAnalytics={() => {
          setView("analytics");
          setMobilePane("main");
        }}
        onModels={() => {
          setView("models");
          setMobilePane("main");
        }}
        onUsers={() => {
          setView("users");
          setMobilePane("main");
        }}
        onSettings={() => {
          setView("settings");
          setMobilePane("main");
        }}
        onSelectPlanning={(s) => {
          setSelectedPlanningSession(s);
          setView("planning");
          setMobilePane("main");
        }}
        onNewPlanning={() => {
          setSelectedPlanningSession(null);
          setView("planning");
          setMobilePane("main");
        }}
        onDeleted={(taskId) => {
          setTasks((t) => t.filter((task) => task.task_id !== taskId));
          if (selected?.task_id === taskId) {
            setSelected(null);
            setView("planning");
            setMobilePane("list");
          }
        }}
        onPlanningDeleted={(sessionId) => {
          setPlanningSessions((list) => list.filter((s) => s.session_id !== sessionId));
          if (selectedPlanningSession?.session_id === sessionId) {
            // The open conversation just went away; drop the selection rather
            // than leaving the pane bound to a session the server no longer has.
            setSelectedPlanningSession(null);
            setMobilePane("list");
          }
        }}
        onLogout={onLogout}
      />
      <div className="main-pane">
        <div className="mobile-topbar">
          <button className="mobile-back" onClick={() => setMobilePane("list")} aria-label="Back to tasks">
            <Icon name="chevronLeft" size={18} />
            <span>Tasks</span>
          </button>
          <span className="mobile-topbar-title">{viewTitle}</span>
        </div>
        {view === "task" && selected && <TaskView task={selected} stream={taskStream} setGeneration={setGeneration} />}
        {view === "new-task" && <NewTaskPanel repos={repos} onSubmit={handleCreate} submitting={submitting} error={createError} onClearError={() => setCreateError(null)} />}
        {view === "analytics" && user.role === "admin" && (
          <Suspense fallback={<div style={{ padding: "2rem", color: "var(--text-muted, #888)" }}>Loading analytics...</div>}>
            <AnalyticsView />
          </Suspense>
        )}
        {view === "models" && user.role === "admin" && (
          /* Single wrapper on purpose: .main-pane gives `flex: 1` to EVERY direct
             child, so returning two siblings here split the pane 50/50 and blew
             the status card up to half the screen. The wrapper takes that flex
             slot; inside it the card sizes to its content and the model list
             takes the remaining height. */
          <div className="models-view">
            <ConsolidationStatusPanel />
            <ModelConfigPanel />
          </div>
        )}
        {view === "users" && user.role === "admin" && <UsersPanel repos={repos} />}
        {view === "settings" && <SettingsPage user={user} onUserChanged={onUserChanged} />}
        {view === "planning" && (
          <PlanningView
            key={selectedPlanningSession?.session_id ?? "new"}
            repos={repos}
            session={selectedPlanningSession}
            onBuildNow={(goal, repo, budgetUsd) => handleCreate(goal, repo, budgetUsd, [])}
            onSessionCreated={(s) => {
              setPlanningSessions((list) => [s, ...list]);
              setSelectedPlanningSession(s);
            }}
          />
        )}
      </div>
      {/* Bottom tab bar, mobile only. Navigation used to live solely in the
          sidebar — which IS the list pane on a phone — so reaching Analytics
          took three gestures. These sit in the thumb zone instead. */}
      <MobileNav
        view={view}
        pane={mobilePane}
        isAdmin={user.role === "admin"}
        onTasks={() => setMobilePane("list")}
        onNewTask={() => { setView("new-task"); setMobilePane("main"); }}
        onAnalytics={() => { setView("analytics"); setMobilePane("main"); }}
        onModels={() => { setView("models"); setMobilePane("main"); }}
        onSettings={() => { setView("settings"); setMobilePane("main"); }}
      />
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    getMe()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecked(true));
  }, []);

  // audit H-14: any authenticated request that comes back 401 (expired/invalid
  // cookie) clears the user here, dropping the app straight back to the login
  // screen instead of degrading into stale data and opaque "... failed: 401".
  useEffect(() => {
    setAuthFailureHandler(() => setUser(null));
    return () => setAuthFailureHandler(null);
  }, []);

  async function handleLogout() {
    await logout();
    setUser(null);
  }

  // audit M-20: a visible loading state during the initial getMe(), so a slow
  // auth check is distinguishable from a crashed render (the ErrorBoundary now
  // catches the latter and shows its own fallback).
  if (!checked) {
    return (
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        minHeight: "100vh", color: "var(--text-muted, #888)",
        fontFamily: "system-ui, sans-serif", fontSize: "0.9rem",
      }}>
        Loading...
      </div>
    );
  }
  if (!user) return <LoginPage onLoggedIn={setUser} />;
  if (user.must_change_password) {
    return <ChangePasswordPage onDone={() => setUser({ ...user, must_change_password: false })} />;
  }
  if (user.require_totp_setup) {
    return <SetupTotpPage onDone={() => setUser({ ...user, require_totp_setup: false, totp_enabled: true })} />;
  }
  return <AuthenticatedApp user={user} onLogout={handleLogout} onUserChanged={setUser} />;
}
