// audit M-23: shared task-category constants, split out of AnalyticsView so a
// consumer (Sidebar) can import them WITHOUT statically pulling recharts into
// the main bundle -- that static import was making AnalyticsView's dynamic
// import ineffective (recharts shipped to every user regardless).
export const CATEGORY_ORDER = ["bug-fix", "feature", "ui-styling", "performance", "investigation", "other"];
export const CATEGORY_LABELS: Record<string, string> = {
  "bug-fix": "Bug fix",
  feature: "Feature",
  "ui-styling": "UI/styling",
  performance: "Performance",
  investigation: "Investigation",
  other: "Other",
};
