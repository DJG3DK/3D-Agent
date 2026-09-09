"""The draft gate and the read window floor (agent/tools/planning_tools.py).
A planning turn may read a bounded number of files before it must save a
plan; paged reads never come back smaller than 500 lines."""

import shutil

import pytest

import agent.runtime_settings as rs
import agent.tools.planning_tools as planning_tools
from agent.tools.planning_tools import make_planning_tools

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed (the planner's search needs it; install.sh installs it)")


@pytest.fixture
def tools(tmp_path, monkeypatch):
    (tmp_path / "big.tsx").write_text("\n".join(f"line {i}" for i in range(1, 1201)))
    for i in range(12):
        (tmp_path / f"f{i}.ts").write_text(f"export const v{i} = {i};\n")
    monkeypatch.setattr(planning_tools, "PROJECTS", {"demo": {"sandbox": str(tmp_path)}})
    monkeypatch.setattr(rs, "_values", {**rs._values, "planning_read_budget": 5.0})
    t, plan_ref = make_planning_tools()
    return {x.name: x for x in t}, plan_ref


def test_paged_reads_return_at_least_500_lines(tools):
    t, _ = tools
    out = t["read_project_file"].invoke({"repo": "demo", "path": "big.tsx", "offset": 100, "limit": 120})
    assert "[lines 100-599 of 1200" in out, out[-80:]


def test_a_bigger_window_is_honoured(tools):
    t, _ = tools
    out = t["read_project_file"].invoke({"repo": "demo", "path": "big.tsx", "offset": 1, "limit": 800})
    assert "[lines 1-800 of 1200" in out


def test_reads_close_at_the_budget_until_a_plan_is_saved(tools):
    t, plan_ref = tools
    for i in range(5):
        assert not t["read_project_file"].invoke({"repo": "demo", "path": f"f{i}.ts"}).startswith("ERROR")
    closed = t["read_project_file"].invoke({"repo": "demo", "path": "f5.ts"})
    assert closed.startswith("ERROR: 5 file reads since the last saved plan")
    assert "save_plan" in closed and "search_project" in closed
    # still closed on retry, and it costs nothing
    assert t["read_project_file"].invoke({"repo": "demo", "path": "f6.ts"}).startswith("ERROR")
    t["save_plan"].invoke({"markdown": "# draft\n\nopen question: f6"})
    assert plan_ref["markdown"].startswith("# draft")
    assert not t["read_project_file"].invoke({"repo": "demo", "path": "f6.ts"}).startswith("ERROR"), "reads reopen after a save"


def test_search_is_not_counted_against_the_read_budget(tools, tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    t, _ = tools
    for _ in range(6):
        t["search_project"].invoke({"repo": "demo", "pattern": "export", "path": "."})
    assert not t["read_project_file"].invoke({"repo": "demo", "path": "f0.ts"}).startswith("ERROR")


def test_knob_is_on_the_settings_page():
    assert rs.KNOBS["planning_read_budget"]["default"] == 50


def test_a_gate_forced_save_tells_the_model_to_keep_working(tools):
    """2026-09-09: hit the gate, saved, one more read, turn over -- because
    save_plan replied "Plan saved, use Build Now", which reads as done."""
    t, _ = tools
    for i in range(5):
        t["read_project_file"].invoke({"repo": "demo", "path": f"f{i}.ts"})
    assert t["read_project_file"].invoke({"repo": "demo", "path": "f5.ts"}).startswith("ERROR: 5 file reads")
    reply = t["save_plan"].invoke({"markdown": "# draft\n\nopen question: f6"})
    assert reply.startswith("Draft saved (") and "read 5 more files" in reply
    assert "keep working in this same turn" in reply and "save_plan again" in reply
    # a save that was NOT forced keeps the plain reply
    assert t["save_plan"].invoke({"markdown": "# final"}) == 'Plan saved. The user can now see it and use "Build Now" whenever they\'re ready.'


def test_the_gate_message_calls_the_save_a_checkpoint(tools):
    t, _ = tools
    for i in range(5):
        t["read_project_file"].invoke({"repo": "demo", "path": f"f{i}.ts"})
    msg = t["read_project_file"].invoke({"repo": "demo", "path": "f5.ts"})
    assert "DRAFT" in msg and "checkpoint, not the end of the turn" in msg
