"""The operator's final-look gate: verify_and_ship must park a READY commit
when the task requires merge review, and the router must treat that park as a
resting state -- then send an approved task back through verify_and_ship so
the one code path that knows how to merge does the merge.
"""
from __future__ import annotations

import pytest
from langgraph.graph import END

from agent.outer_graph import _route_after_verify
from agent.task_diff import parse_numstat, split_patch


# ── routing ─────────────────────────────────────────────────────────────────

def test_pending_merge_approval_is_a_resting_state():
    assert _route_after_verify({"pending_merge_approval": {"sha": "abc"}}) == END


def test_approved_outstanding_commit_reenters_verify():
    state = {"merge_approved_sha": "abc", "committed_sha": "abc"}
    assert _route_after_verify(state) == "verify_and_ship"


def test_approval_without_outstanding_commit_ends():
    # Merge already happened (committed_sha cleared) -- nothing to re-run.
    assert _route_after_verify({"merge_approved_sha": "abc", "committed_sha": None}) == END


def test_escalation_outranks_merge_approval():
    state = {"escalated": True, "pending_merge_approval": {"sha": "abc"}}
    assert _route_after_verify(state) == END


def test_feedback_still_routes_to_work():
    assert _route_after_verify({"pending_feedback": "notes"}) == "work"


# ── the gate's sha equality ─────────────────────────────────────────────────

def test_state_defaults_include_merge_fields():
    from agent.outer_state import initial_state
    st = initial_state(task_id="t", goal="g", repo="r", budget_usd=1.0)
    assert st["require_merge_review"] is True, "final look is the DEFAULT; bypass is the opt-in"
    assert st["pending_merge_approval"] is None
    assert st["merge_approved_sha"] is None


def test_bypass_is_captured_at_creation():
    from agent.outer_state import initial_state
    st = initial_state(task_id="t", goal="g", repo="r", budget_usd=1.0, require_merge_review=False)
    assert st["require_merge_review"] is False


# ── diff parsing (pure) ─────────────────────────────────────────────────────

def test_split_patch_splits_on_file_boundaries():
    text = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/dir/y.js b/dir/y.js\n--- a/dir/y.js\n+++ b/dir/y.js\n@@ -0,0 +1 @@\n+new\n"
    )
    out = split_patch(text)
    assert set(out) == {"x.py", "dir/y.js"}
    assert "+b" in out["x.py"] and "+new" in out["dir/y.js"]


def test_split_patch_handles_paths_with_spaces():
    text = "diff --git a/my file.md b/my file.md\n--- a/my file.md\n+++ b/my file.md\n@@ @@\n+x\n"
    assert "my file.md" in split_patch(text)


def test_parse_numstat_marks_binary_as_none():
    out = parse_numstat("12\t3\tsrc/a.py\n-\t-\tlogo.png\n")
    assert out["src/a.py"] == (12, 3)
    assert out["logo.png"] == (None, None)


def test_failed_deploy_must_not_route_back_into_verify():
    """The wedge of 2026-08-26: a failed build kept merge_approved_sha set, so
    approved==committed outranked pending_feedback in the router and the
    loop-back never reached work. With the approval consumed on failure, this
    state routes to work; and even if a stale approval survives, the fast
    path refuses to fire while feedback is queued."""
    state = {
        "merge_approved_sha": None,          # consumed by the failed attempt
        "committed_sha": "abc",
        "pending_feedback": "the build failed; fix the code",
    }
    assert _route_after_verify(state) == "work"


def test_stale_approval_with_feedback_routes_to_work_not_a_merge_loop():
    # audit C-5 (re-verification): a fresh review returning NEEDS_FIXES for an
    # already-approved sha leaves BOTH merge_approved_sha and pending_feedback
    # set. The earlier "fast path guard" inside verify_and_ship refused the fast
    # merge but did NOT break the loop -- the router still checked
    # merge_approved_sha first and sent it back into verify_and_ship every lap
    # (a fresh trigger_check + up to 900s wait each time) until the iteration
    # ceiling. The router now checks pending_feedback FIRST, and the NEEDS_FIXES
    # return clears merge_approved_sha, so this reaches `work` and acts on the
    # feedback.
    state = {"merge_approved_sha": "abc", "committed_sha": "abc",
             "pending_feedback": "fix it"}
    assert _route_after_verify(state) == "work"
