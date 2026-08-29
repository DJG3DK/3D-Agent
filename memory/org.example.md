# Org-wide conventions

These apply across every project this agent works on, not just one repo.

## Testing

- Tests must exercise real behavior, not inspect a function's source as a string. A test
  asserting on `fn.toString()` passes even when the underlying logic is broken.
- A test file must be wired into the project's aggregate test command, not just written.

## Deterministic checks

- A failing check (typecheck/lint/test) must be investigated and fixed, not dismissed as
  unrelated or pre-existing without confirming that first.

## State-changing operations

- Any multi-step operation that can fail partway through must verify each step succeeded
  before persisting anything that claims success.

## General

- Reuse existing hardened helpers (shell execution, file I/O, git operations) instead of
  re-implementing similar functionality.
