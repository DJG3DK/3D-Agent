"""The check suite runs with no network, so network errors need explaining.

`run_check` uses `--network none` deliberately: it executes agent-authored
test code, which must not have egress. The consequence is that any test
reaching the internet fails with a bare `getaddrinfo EAI_AGAIN <host>`, which
reads exactly like a code regression.

Observed live (2026-08-29): a suite reported 703/704 passing with the single
failure being a DNS lookup from an SSRF guard inside the code under test. The
test mocked `global.fetch`, but the guard called `dns.lookup()` first, so the
mock was never reached. Both the agent and the operator had to reason their
way to "this is the sandbox".

The annotation explains; it must never suppress.
"""

import pytest

from agent.tools import checks


@pytest.mark.parametrize("output", [
    "FAIL src/x.spec.ts\n  getaddrinfo EAI_AGAIN example.com",
    "Error: getaddrinfo ENOTFOUND registry.npmjs.org",
    "curl: (6) Could not resolve host: example.com",
    "connect ECONNREFUSED 93.184.216.34:443",
    "ping: Temporary failure in name resolution",
    "fetch failed: Network is unreachable",
])
def test_network_failures_are_explained(output):
    annotated = checks._annotate_offline_failure(output)
    assert "NOTE FROM THE CHECK RUNNER" in annotated
    assert "--network none" in annotated
    assert output in annotated, "the original output must still be present in full"


@pytest.mark.parametrize("output", [
    "AssertionError: expected 1 to be 2",
    "TypeError: Cannot read properties of undefined",
    "  ● should render the banner\n    Expected: pink\n    Received: lavender",
    "",
])
def test_ordinary_failures_are_left_alone(output):
    """A note on every failure would be noise, and would train the reader to
    skip it — which is the one thing that must not happen to this note."""
    assert checks._annotate_offline_failure(output) == output


def test_the_annotation_names_the_actual_trap():
    """The specific miss that caused this: mocking the fetch layer when the
    code resolves DNS first. Someone reading the note should not have to
    rediscover that."""
    note = checks._annotate_offline_failure("getaddrinfo EAI_AGAIN example.com")
    assert "global.fetch" in note
    assert "SSRF" in note or "resolves DNS first" in note


def test_annotation_does_not_change_the_verdict():
    """It explains a failure; it must not convert one into a pass. The `ok`
    flag is computed from the exit code and never from this text."""
    import inspect
    src = inspect.getsource(checks.run_check)
    assert '"ok": result["ok"]' in src, (
        "the check verdict must come from the process exit code, not from the "
        "annotated output")
