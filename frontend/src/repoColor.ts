// Deterministic color per repo name, so the badge/chart color for a repo is
// stable across reloads without hardcoding the set of configured repos here.
const PALETTE = [
  "#58a6ff",
  "#8b7cf6",
  "#e3a72e",
  "#3fb950",
  "#f85149",
  "#3fb9a5",
  "#e3702e",
  "#a56ef5",
];

export function repoColor(repo: string): string {
  let hash = 0;
  for (let i = 0; i < repo.length; i++) {
    hash = (hash * 31 + repo.charCodeAt(i)) | 0;
  }
  return PALETTE[Math.abs(hash) % PALETTE.length];
}
