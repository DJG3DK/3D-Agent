/** Which coder / planner seat a request runs on. "Auto" lets the server decide
 *  from the task's category, the files it names and a few keywords
 *  (agent/frontend_route.py); the other two override it. Frontend is the
 *  Kimi seat, pinned for polish and priced accordingly, so the choice is
 *  always visible rather than buried in settings. */

export type RouteChoice = "auto" | "frontend" | "general";

export function RouteSelect({ value, onChange }: { value: RouteChoice; onChange: (v: RouteChoice) => void }) {
  return (
    <label className="field">
      <span>Model route</span>
      <select value={value} onChange={(e) => onChange(e.target.value as RouteChoice)} title="Frontend work runs on the frontend coder (Kimi); everything else on the general coder">
        <option value="auto">Auto (detect frontend work)</option>
        <option value="frontend">Frontend (Kimi)</option>
        <option value="general">General</option>
      </select>
    </label>
  );
}

/** The badge a task or session shows once the route is decided. */
export function RouteBadge({ route, reason }: { route?: string | null; reason?: string | null }) {
  if (!route) return null;
  const label = route === "frontend" ? "frontend · kimi" : "general";
  return (
    <span className={`route-badge route-badge--${route}`} title={reason ? `Route: ${route} — ${reason}` : `Route: ${route}`}>
      {label}
    </span>
  );
}
