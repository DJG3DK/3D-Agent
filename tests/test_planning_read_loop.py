"""The re-read loop that burned a whole $8 planning turn.

Session 0616917 (2026-08-31) ran 1h52m, made 123 model calls, read 1,080 files
across 99 distinct paths -- src/core/bot.js 129 times, config/pairs.json 103 --
and produced no plan at all before the budget guard stopped it. Summarization
kept evicting what had just been read, so from inside the loop every re-read
looked like a first read.

Two independent defenses, both tested here: a context window wide enough that
the eviction is rare, and a hard per-file ceiling so a loop that big is not
reachable even if it isn't.
"""

import pytest

from agent import deep_agent
from agent.tools import planning_tools


def _read_tool():
    tools, _ = planning_tools.make_planning_tools(allowed_repos=None)
    return next(t for t in tools if t.name == "read_project_file")


@pytest.fixture
def read(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "big.js").write_text("\n".join(f"line {i}" for i in range(1, 400)))
    monkeypatch.setattr(planning_tools, "_project_root", lambda r, a=None: str(repo))
    tool = _read_tool()
    return lambda **kw: tool.invoke({"repo": "demo", "path": "big.js", **kw})


class TestPerFileReadCeiling:
    def test_an_ordinary_read_is_untouched(self, read):
        out = read()
        assert "line 1" in out
        assert "times in this turn" not in out

    def test_a_full_page_through_never_trips_the_cap(self, read):
        # 400 lines in 50-line windows is 8 reads -- a legitimate pattern that
        # must not be mistaken for the loop.
        for offset in range(1, 400, 50):
            out = read(offset=offset, limit=50)
            assert not out.startswith("ERROR"), f"paging broke at offset {offset}"

    def test_it_warns_before_it_refuses(self, read):
        for _ in range(planning_tools._READ_WARN_AT - 1):
            assert "times in this turn" not in read()
        warned = read()
        assert "times in this turn" in warned
        # A warning still returns the file -- it is a nudge, not a refusal.
        assert "line 1" in warned

    def test_the_warning_says_what_to_do_instead(self, read):
        for _ in range(planning_tools._READ_WARN_AT):
            out = read()
        # The model cannot see that its earlier reads were summarized away, so
        # the advice has to be explicit: write it down, don't re-read.
        assert "summarized" in out.lower()
        assert "reply" in out.lower()

    def test_it_refuses_past_the_cap(self, read):
        for _ in range(planning_tools._READ_CAP):
            read()
        refused = read()
        assert refused.startswith("ERROR")
        assert "line 1" not in refused

    def test_the_cap_is_per_file_not_global(self, read, tmp_path):
        for _ in range(planning_tools._READ_CAP + 1):
            read()
        (tmp_path / "repo" / "other.js").write_text("const x = 1;")
        # A different file the model has never opened must still be readable --
        # otherwise one runaway file takes the whole turn down with it.
        assert "const x" in read(path="other.js")

    def test_a_refusal_is_cheap(self, read, monkeypatch):
        for _ in range(planning_tools._READ_CAP):
            read()
        called = []
        monkeypatch.setattr(planning_tools, "read_file", lambda *a, **k: called.append(1) or "")
        read()
        assert not called, "a refused read must not touch the disk"

    def test_a_fresh_tool_set_is_a_fresh_budget(self, tmp_path, monkeypatch):
        # make_planning_tools is called once per turn, so the ledger resets --
        # a session is never permanently locked out of a file.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "big.js").write_text("hello")
        monkeypatch.setattr(planning_tools, "_project_root", lambda r, a=None: str(repo))
        for _ in range(planning_tools._READ_CAP + 1):
            _read_tool().invoke({"repo": "demo", "path": "big.js"})
        assert "hello" in _read_tool().invoke({"repo": "demo", "path": "big.js"})


class TestPlanningContextWindow:
    def test_planning_gets_more_headroom_than_a_build_task(self):
        trigger = dict(deep_agent.PLANNING_SUMMARIZATION_TRIGGER)["tokens"]
        assert trigger > dict(deep_agent.SUMMARIZATION_TRIGGER)["tokens"]

    def test_the_window_holds_more_than_a_handful_of_file_reads(self):
        """The actual defect: 80k trigger - 30k keep = 50k of headroom, and a
        planning read is capped at 40k chars (~12k tokens). Three big reads
        refilled it, so the model could not hold the four files its question
        spanned. Headroom has to be worth more reads than that."""
        trigger = dict(deep_agent.PLANNING_SUMMARIZATION_TRIGGER)["tokens"]
        keep = deep_agent.PLANNING_SUMMARIZATION_KEEP[1]
        read_tokens = planning_tools._READ_INLINE_CAP_CHARS / 3.5  # chars -> tokens
        assert (trigger - keep) / read_tokens >= 6

    def test_the_kept_window_stays_well_under_the_trigger(self):
        """If keep ever approaches trigger, summarization fires before every
        model call and can never get back under -- the degenerate case the
        build-task constants were tuned away from."""
        trigger = dict(deep_agent.PLANNING_SUMMARIZATION_TRIGGER)["tokens"]
        assert deep_agent.PLANNING_SUMMARIZATION_KEEP[1] <= trigger * 0.5

    def test_the_trigger_leaves_room_inside_a_262k_context(self):
        # Every pinned planning model has >=262K; the triggering call and its
        # response both have to fit alongside the batch being summarized.
        assert dict(deep_agent.PLANNING_SUMMARIZATION_TRIGGER)["tokens"] <= 200_000
