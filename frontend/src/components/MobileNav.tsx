import { Icon, type IconName } from "./Icon";
import "./MobileNav.css";

/* Bottom tab bar — mobile only.
 *
 * Navigation previously lived exclusively in the sidebar, which on a phone IS
 * the list pane. So changing view meant: back out to the list, scroll, tap.
 * Three gestures to reach Analytics. A phone's reachable area is the bottom
 * third of the screen, and this puts every primary destination there.
 *
 * "Tasks" is not a view — it returns to the list pane, which is what the task
 * list actually is on mobile. That is why it takes `pane` as well as `view`.
 */
export type NavView = "new-task" | "task" | "analytics" | "models" | "planning" | "users" | "settings";

interface Tab {
  key: string;
  label: string;
  icon: IconName;
  admin?: boolean;
  /** Highlighted when the current view is one of these, or the list is showing. */
  match: (view: NavView, pane: "list" | "main") => boolean;
  go: () => void;
}

export function MobileNav({
  view, pane, isAdmin, onTasks, onNewTask, onAnalytics, onModels, onSettings,
}: {
  view: NavView;
  pane: "list" | "main";
  isAdmin: boolean;
  onTasks: () => void;
  onNewTask: () => void;
  onAnalytics: () => void;
  onModels: () => void;
  onSettings: () => void;
}) {
  const tabs: Tab[] = [
    { key: "tasks", label: "Tasks", icon: "tasks",
      match: (_v, p) => p === "list", go: onTasks },
    { key: "new", label: "New", icon: "plus",
      match: (v, p) => p === "main" && v === "new-task", go: onNewTask },
    { key: "analytics", label: "Stats", icon: "chart", admin: true,
      match: (v, p) => p === "main" && v === "analytics", go: onAnalytics },
    { key: "models", label: "Models", icon: "cpu", admin: true,
      match: (v, p) => p === "main" && v === "models", go: onModels },
    { key: "settings", label: "Settings", icon: "settings",
      match: (v, p) => p === "main" && v === "settings", go: onSettings },
  ];

  const visible = tabs.filter((t) => !t.admin || isAdmin);

  return (
    <nav className="mnav" aria-label="Primary">
      {visible.map((t) => {
        const active = t.match(view, pane);
        return (
          <button
            key={t.key}
            className={`mnav-tab${active ? " is-active" : ""}`}
            onClick={t.go}
            aria-current={active ? "page" : undefined}
          >
            <span className="mnav-ico"><Icon name={t.icon} size={20} /></span>
            <span className="mnav-label">{t.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
