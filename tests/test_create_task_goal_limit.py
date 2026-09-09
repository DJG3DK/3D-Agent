"""A Build Now sends the whole plan as the goal; the limit must clear a real plan."""

import pytest
from pydantic import ValidationError

from agent.server import CreateTaskRequest


def test_a_twenty_thousand_char_plan_is_accepted():
    # 20,364 chars was rejected with 422 on 2026-09-09 and the button just did nothing.
    CreateTaskRequest(goal="x" * 30_000, repo="demo")


def test_the_limit_still_exists():
    with pytest.raises(ValidationError):
        CreateTaskRequest(goal="x" * 90_000, repo="demo")
