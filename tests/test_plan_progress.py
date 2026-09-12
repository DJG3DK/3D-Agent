"""A plan counter that cannot go backwards.

Live, 2026-09-12, task 01e640ef: twelve items with six completed became a
fresh six-item list with nothing completed, because the coordinator answered
"update your plan" by writing what was LEFT -- which is a legitimate use of a
tool whose contract is to replace the list. The worktree already held 23
changed files and 967 deleted lines. What the operator saw was 6/12 fall to
0/6, twice, and the reasonable conclusion was that the task was looping.

The strip is not the only reader. The commit gate reads latest_todos to decide
whether the plan is finished, and incomplete_plan_streak escalates a task
whose plan never completes -- so an all-pending rewrite can hold a finished
task open. Both reasons to merge rather than replace.
"""

from agent.plan_progress import counts, merge_todos


def _t(content, status="pending"):
    return {"content": content, "status": status}


def test_the_live_shape_a_rewrite_that_drops_what_was_finished():
    before = [_t("core removal", "completed"), _t("mirror in backtester", "completed"),
              _t("frontend types", "in_progress"), _t("docs", "pending")]
    after = [_t("frontend types", "in_progress"), _t("docs", "pending"),
             _t("regenerate fixtures", "pending")]

    merged = merge_todos(before, after)

    assert counts(merged) == (2, 5), "the two finished items survive the rewrite"
    assert [t["content"] for t in merged[:2]] == ["core removal", "mirror in backtester"]
    assert [t["content"] for t in merged[2:]] == [t["content"] for t in after], \
        "and the model's own list follows, in its order"


def test_an_item_that_comes_back_as_pending_stays_completed():
    """The status is the one thing a rewrite does not get to undo."""
    before = [_t("delete the sweep module", "completed")]
    merged = merge_todos(before, [_t("delete the sweep module", "pending")])
    assert merged == [{"content": "delete the sweep module", "status": "completed"}]


def test_rewording_and_reordering_are_the_models_to_make():
    before = [_t("a", "completed"), _t("b", "pending")]
    after = [_t("b", "in_progress"), _t("a", "completed"), _t("c", "pending")]
    merged = merge_todos(before, after)
    assert [t["content"] for t in merged] == ["b", "a", "c"], "the model's order wins"
    assert counts(merged) == (1, 3)


def test_matching_ignores_whitespace_and_case():
    before = [_t("Regenerate  tests/fixtures/baseline_trades.json", "completed")]
    after = [_t("regenerate tests/fixtures/baseline_trades.json")]
    assert merge_todos(before, after)[0]["status"] == "completed"


def test_a_genuinely_new_plan_is_not_invented_around():
    """Only completed items are carried. A pending item the model dropped is
    dropped -- re-planning is allowed, un-finishing is not."""
    before = [_t("old idea", "pending"), _t("done thing", "completed")]
    merged = merge_todos(before, [_t("new idea")])
    assert [t["content"] for t in merged] == ["done thing", "new idea"]


def test_nothing_written_this_turn_leaves_the_plan_alone():
    before = [_t("a", "completed")]
    assert merge_todos(before, None) == before


def test_the_first_plan_passes_through_untouched():
    first = [_t("a"), _t("b", "in_progress")]
    assert merge_todos(None, first) == first
    assert merge_todos([], first) == first


def test_malformed_entries_never_raise():
    before = [_t("a", "completed"), "not a dict", {"no_content": True}]
    after = ["junk", _t("a"), {"content": "", "status": "pending"}]
    merged = merge_todos(before, after)
    assert any(isinstance(t, dict) and t.get("content") == "a"
               and t["status"] == "completed" for t in merged)
    assert counts("not a list") == (0, 0)


def test_counts_is_what_the_strip_shows():
    assert counts([_t("a", "completed"), _t("b", "in_progress"), _t("c")]) == (1, 3)
    assert counts(None) == (0, 0)


def test_the_denominator_never_shrinks_across_a_sequence_of_rewrites():
    """The property the operator actually cares about: twelve items, finished
    a few at a time, each rewrite listing only the rest."""
    plan = [_t(f"item {i}") for i in range(12)]
    state = merge_todos(None, plan)
    for batch in range(0, 12, 3):
        remaining = [_t(f"item {i}") for i in range(batch + 3, 12)]
        state = merge_todos(
            [{**t, "status": "completed"} if t["content"] in
             {f"item {i}" for i in range(batch + 3)} else t for t in state],
            remaining,
        )
        done, total = counts(state)
        assert total == 12, f"the plan shrank to {total} after batch {batch}"
        assert done == batch + 3, f"progress read {done} after finishing {batch + 3}"
