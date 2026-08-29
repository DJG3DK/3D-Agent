import { repoColor } from "../repoColor";
import "./RepoBadge.css";

export function RepoBadge({ repo }: { repo: string }) {
  const color = repoColor(repo);
  return (
    <span className="repo-badge" style={{ color, background: `${color}1a`, borderColor: `${color}40` }}>
      {repo}
    </span>
  );
}
