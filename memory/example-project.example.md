# example-project memory

Template for a single project's memory file. Real deployments keep one file per project
in this directory (gitignored) — this file documents the shape only.

## Conventions

Record durable, non-obvious facts about the codebase: naming conventions, concurrency
primitives, where a particular kind of change needs to happen.

## Known environment gotchas

Record anything about the sandbox/CI environment that has caused a task to fail or waste
time for reasons unrelated to the actual task — a missing build step, a test that only
works against one specific live instance, a dependency that needs a fresh install.

Keep entries short and specific. This file grows from real tasks, not speculation.
