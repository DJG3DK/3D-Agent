"""Test runs must never trace to the production LangSmith project -- a
synthetic fixture conversation landing in the same project the live agent
traces to is indistinguishable from real pathology at a glance, and it also
skews the analytics scan's usage numbers.

Set before any langchain import (conftest is imported first), and
load_dotenv never overrides already-set env vars, so .env can't re-enable
it mid-suite.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import pytest

from agent.config import PROJECTS


@pytest.fixture(autouse=True)
def _test_repo_project(tmp_path_factory):
    """Ensures a "test-repo" entry exists in PROJECTS for the duration of
    each test, so unit tests can use a fixture repo name without depending
    on this deployment's real projects.json contents. Restored afterward so
    tests don't leak state into each other."""
    had_entry = "test-repo" in PROJECTS
    previous = PROJECTS.get("test-repo")
    sandbox = tmp_path_factory.mktemp("test-repo-sandbox")
    PROJECTS["test-repo"] = {"sandbox": str(sandbox), "live": str(sandbox)}
    yield
    if had_entry:
        PROJECTS["test-repo"] = previous
    else:
        PROJECTS.pop("test-repo", None)
