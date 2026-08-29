import type { PlanStep } from "../types";
import "./PlanTracker.css";

const ICON: Record<PlanStep["status"], string> = {
  pending: "○",
  in_progress: "◐",
  done: "✓",
  failed: "✕",
  skipped: "–",
};

export function PlanTracker({ plan }: { plan: PlanStep[] }) {
  if (plan.length === 0) return null;
  const done = plan.filter((s) => s.status === "done").length;

  return (
    <div className="plan-tracker">
      <div className="plan-tracker-summary">
        {done}/{plan.length} steps
      </div>
      <div className="plan-tracker-steps">
        {plan.map((step, i) => (
          <div key={step.id} className={`plan-step plan-step-${step.status}`} title={step.description}>
            <span className="plan-step-icon">{ICON[step.status]}</span>
            <span className="plan-step-index">{i + 1}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
