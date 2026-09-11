# Add a GitHub inbox source

A **source** is one thing the poller watches on a repository: Dependabot PRs,
security alerts, CodeQL findings, failing checks on the default branch,
reviews requesting changes. Each is switchable per project to `off`,
`propose` or `auto`.

A half-added source is worse than none. The settings card draws a column for
every name in the registry, so the switch appears immediately — and if
`discover()` never looks for that name, the operator sets it to Auto and
nothing is ever found. Nothing found looks exactly like nothing to do.

## Run this first

```bash
.venv/bin/python -m pytest tests/test_github_inbox.py tests/test_github_settings.py -q
```

Add your name to `SOURCES` and it turns into a list of everything still
missing — this is the real output from adding a placeholder source:

```
FAILED tests/test_github_inbox.py::test_every_source_in_the_registry_is_one_discover_looks_for
FAILED tests/test_github_inbox.py::test_every_source_can_become_a_task_and_a_notification[stale_branches]
FAILED tests/test_github_settings.py::test_readme_documents_every_inbox_source
FAILED tests/test_github_settings.py::test_the_settings_card_can_render_every_source
FAILED tests/test_github_settings.py::test_the_frontend_type_knows_every_source
```

Five failures, five places to wire. Work them off in order.

## The edits

1. **`agent/github_settings.py` → `SOURCES`** — `label` and `help`. The help
   text is what the operator hovers before switching something to Auto, so
   say what the task will *do*, not only what is watched. New sources default
   to `off` for every existing project (`normalize` fills the gap), which is
   the right default: a new switch never turns itself on.

2. **`agent/github_inbox.py` → `discover()`** — a `policies[...] != "off"`
   branch that appends `Item(kind="<your source>", ...)`. Two properties the
   existing branches all have and yours needs:
   - **Wrap it in its own `try`.** One source failing — a token without the
     permission, most often — must not hide the others. Log and continue.
   - **Make `fingerprint` change when the work changes and not otherwise.**
     It is how `decide()` tells "still the same item" from "this came back
     different"; a fingerprint that moves on every poll re-proposes forever,
     and one that never moves misses a real update.

3. **`agent/github_inbox.py` → `_GOAL_TEMPLATES` and `_KIND_LABEL`** — the
   task goal and the notification label. `build_goal` raises `KeyError` on an
   unknown kind, inside the poller, where it reads as the whole project
   failing to poll.

4. **`frontend/src/api.ts` → `GitHubSource`** and
   **`frontend/src/components/GitHubSettingsCard.tsx` → `SOURCE_ORDER`** —
   the union type and the column order. Both are hand-written; the API sends
   the source either way, and the table simply never draws it.

5. **`README.md`** — the inbox table. A source that exists only in code is a
   feature nobody knows they have.

## Verify

```bash
.venv/bin/python -m pytest tests/test_github_inbox.py tests/test_github_settings.py -q
cd frontend && npx tsc --noEmit -p tsconfig.app.json && npm test
```

Then, on a real deployment: switch the new source to `propose` for one
project, press **Poll now** on the GitHub tab, and confirm an item appears
with a sensible title. `auto` can wait until `propose` has been right twice.

## What the gate still enforces

An inbox task is a task. It gets the project's normal budget, it goes through
the review gate, and **merge review is forced on** regardless of the
operator's own setting — `_github_create_task` passes
`require_merge_review=True` and `auto_approve_commands=False`, and
`tests/test_inbox_task_invariants.py` holds that. Auto also refuses a project
whose checks list is empty: without checks, "auto" would mean shipping work
that nothing verified.
