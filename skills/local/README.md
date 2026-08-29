# Local skills

Deployment-specific skills live here and are **gitignored** — this is where
your own domain knowledge goes: architecture notes for your repos, house
conventions, anything that names your systems.

Each skill is a directory containing `SKILL.md` with YAML frontmatter:

```markdown
---
name: my-service-architecture
description: How my-service works -- read before touching src/core/. One or two sentences; this line is what the agent sees when deciding whether to open the file.
---

# My Service Architecture
...
```

`scripts/seed_skills.py` discovers every `skills/local/*/SKILL.md`
automatically and seeds it into your project stores. By default a local skill
goes to every project; to scope one, add `targets.json` here:

```json
{ "my-service-architecture": ["my-service"] }
```

Keep `SKILL.md` under ~500 lines (the Agent Skills spec's guidance) — the
agent pays for the whole file every time it opens one, so specific and short
beats comprehensive.
