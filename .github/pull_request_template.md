## What this changes

<!-- One or two sentences. If it fixes an issue, link it. -->

## Why

<!-- What was wrong, or what became possible. If this fixes something that
     failed in a real deployment, say what failed — that context belongs in
     the code comment too. -->

## How it was verified

<!-- Which suites you ran, and anything you exercised by hand.
     `.venv/bin/python -m pytest -q`
     `cd frontend && npx tsc --noEmit -p tsconfig.app.json && npm run lint`
     `node tests/test_projects_config_merge.js`  -->

## What breaks if this is wrong

<!-- Be honest here; reviewers calibrate on it. -->

---

- [ ] A test fails without this change (for behaviour changes)
- [ ] No secrets, personal paths or real credentials in the diff
- [ ] I agree to license this contribution under PolyForm Noncommercial 1.0.0
