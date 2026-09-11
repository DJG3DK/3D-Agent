# Playbooks

Three things get added to this system often enough to be worth a recipe: a
model role, a GitHub inbox source, and a runtime knob. Each of them is wired
into four or five places, and each place is silent when you miss it — a role
the Models page will not show, a source whose switch finds nothing forever, a
dial that drives nothing.

So none of these playbooks is a checklist you are trusted to follow. Each one
starts by running a test that **fails until you are finished**, and the test's
message names the file you have not touched yet. Read the failure, not the
list.

| Playbook | Start by running |
|---|---|
| [Add a managed role](add-a-managed-role.md) | `pytest tests/test_managed_roles_complete.py` |
| [Add an inbox source](add-an-inbox-source.md) | `pytest tests/test_github_inbox.py tests/test_github_settings.py` |
| [Add a runtime knob](add-a-runtime-knob.md) | `pytest tests/test_runtime_settings.py` |

That is the extension API of this repo: a registry, and a test that walks the
registry and checks every consumer handled it. If you add a fourth kind of
extension point, add its completeness test in the same shape — a loop over the
registry, one assertion per consumer, and a failure message that says what
breaks for the operator rather than what is unequal.

Two things no playbook covers, deliberately:

- **`agent/server.py` is not to be flattened in one pass.** If it is split,
  split along the seams that already exist (auth, tasks, planning, github,
  settings, uploads) and keep `tests/test_route_inventory.py` green through
  every step. A big-bang split loses the incident comments, which are the only
  record of why half the routes behave the way they do.
- **Middleware** has no playbook because it has no registry: each agent builds
  its own chain by hand. [docs/middleware.md](../middleware.md) is the
  inventory, and `tests/test_middleware_inventory.py` keeps it honest.
