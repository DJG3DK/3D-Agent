# Add a runtime knob

A **knob** is an operator-tunable limit that lives in Postgres rather than in
the image: read budgets, loop caps, per-turn ceilings. They exist because the
alternative was a file edit plus a restart, and a restart is exactly what you
cannot do while the thing you want to retune is running.

Every knob is read **at the point of use**, so a change lands on the next turn
or task and never mutates something already in flight. A task that started
under a $2 ceiling finishes under it.

## Run this first

```bash
.venv/bin/python -m pytest tests/test_runtime_settings.py -q
```

Add an entry to `KNOBS` and the wiring test names it:

```
AssertionError: knobs exposed in the UI but read by nothing: ['playbook_probe']
```

A dial that drives nothing is worse than no dial: it invites someone to change
it and conclude the system ignores them.

## The edits

1. **`agent/runtime_settings.py` → `KNOBS`** — `label`, `help`, `unit`,
   `default`, `min`, `max`, `group`, and `env` if this value used to come
   from an environment variable (keep it, so an existing deployment's setting
   is not silently discarded on upgrade). The bounds are not decoration:
   values are **clamped, never rejected**, so `min`/`max` are the only thing
   standing between a typo and a knob set to zero.

2. **The call site** — `_rs.as_int("your_knob")` or `rs.value("your_knob")`,
   read where the work happens, not captured at import. Capturing it at
   import is how a knob becomes read-only until the next restart, which is
   the whole thing this mechanism exists to avoid.

3. **Nothing in the frontend.** `RuntimeLimitsPanel` renders whatever the API
   sends and groups by the `group` field. This is the one extension point
   with no UI edit — if you find yourself adding a case to the panel, the
   knob probably wants to be something else.

4. **`help`** — write it for the operator at 2am, not for the reviewer. Say
   what happens when the limit is reached, in the words they will see:
   "past it, `read_project_file` answers 'save the plan now' and reopens
   after `save_plan`" beats "maximum reads".

## Verify

```bash
.venv/bin/python -m pytest tests/test_runtime_settings.py -q
cd frontend && npm test -- RuntimeLimitsPanel
```

Then on a real deployment: change it on the Settings page, and confirm the
next task picks it up with **no restart**. If it needed a restart, it was
captured at import — fix that, not the test.

## What does not belong here

Anything that changes what the system is *allowed to do*. Auto-approve and
merge review are per-user safety switches with their own endpoints and their
own reasoning; these are dials on how hard the agent tries before giving up.
Mixing the two would put a safety boundary behind a slider labelled
"Budgets & loop limits".
