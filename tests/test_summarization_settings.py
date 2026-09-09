"""The summarization trigger must not contain a clause that the keep window
can satisfy on its own -- otherwise summarization fires before every model
call forever (agent/deep_agent.py, SUMMARIZATION_TRIGGER comment, 2026-09-09)."""

from agent.deep_agent import (
    PLANNING_SUMMARIZATION_KEEP, PLANNING_SUMMARIZATION_TRIGGER, SUMMARIZATION_KEEP, SUMMARIZATION_TRIGGER,
)


def _check(trigger, keep):
    keep_kind, keep_value = keep
    for kind, value in trigger:
        if kind == keep_kind:
            assert value > keep_value, f"trigger {kind}={value} must exceed keep {keep_value}"
        else:
            assert kind != "messages" or keep_kind == "messages", (
                "a message-count trigger against a token-count keep can be re-satisfied by the kept "
                "window alone, which makes summarization fire on every call"
            )


def test_build_trigger_cannot_be_satisfied_by_its_keep_window():
    _check(SUMMARIZATION_TRIGGER, SUMMARIZATION_KEEP)


def test_planning_trigger_cannot_be_satisfied_by_its_keep_window():
    _check(PLANNING_SUMMARIZATION_TRIGGER, PLANNING_SUMMARIZATION_KEEP)
