"""search_project / find_files: ripgrep over the real repo for the planner,
with the loop-proofing the hidden built-in grep never had -- a per-turn
budget, a repeat guard, and zero-hit results that say what to change."""

import subprocess

import pytest

import agent.runtime_settings as rs
from agent.tools.planning_tools import make_planning_tools, run_find, run_search


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "frontend" / "src" / "components").mkdir(parents=True)
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "index.css").write_text(":root { --bg: #0b0f14; --accent: #3fb950; }\n")
    (tmp_path / "frontend" / "src" / "components" / "StatCard.tsx").write_text("export const StatCard = () => <div className=\"stat-card\">x</div>;\n// accent color used here\n")
    (tmp_path / "src" / "core" / "bot.js").write_text("const accent = 'none';\nmodule.exports = {};\n")
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("accent accent accent\n")
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _tools(monkeypatch, repo):
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", {"demo": {"sandbox": str(repo)}})
    tools, _ = make_planning_tools()
    return {t.name: t for t in tools}


def test_search_returns_repo_relative_hits_and_skips_ignored_dirs(repo):
    out = run_search(str(repo), "accent")
    assert "frontend/src/index.css:1:" in out
    assert "src/core/bot.js:1:" in out
    assert "node_modules" not in out, "gitignored trees never appear"
    assert out.startswith("3 hit(s) for 'accent'")


def test_search_narrows_by_path_and_glob(repo):
    assert "src/core/bot.js" not in run_search(str(repo), "accent", path="frontend")
    out = run_search(str(repo), "accent", glob="*.css")
    assert "index.css" in out and "StatCard.tsx" not in out


def test_zero_hits_come_back_with_a_diagnosis(repo):
    out = run_search(str(repo), "nonexistent_symbol", path="frontend")
    assert out.startswith("No matches for 'nonexistent_symbol' under 'frontend'")
    assert "files scanned" in out and "search from '.'" in out


def test_an_invalid_regex_points_at_fixed(repo):
    out = run_search(str(repo), "className=(", path="frontend")
    assert out.startswith("ERROR: 'className=(' is not a valid regex") and "fixed=True" in out


def test_zero_hits_with_regex_characters_also_suggest_fixed(repo):
    out = run_search(str(repo), "foo.bar()", path="frontend")
    assert out.startswith("No matches") and "fixed=True" in out


def test_fixed_search_matches_literals(repo):
    assert "StatCard.tsx" in run_search(str(repo), "className=\"stat-card\"", fixed=True)


def test_find_files_is_gitignore_aware(repo):
    out = run_find(str(repo), "**/*.js")
    assert "src/core/bot.js" in out and "node_modules" not in out
    assert run_find(str(repo), "**/*.css").startswith("1 file(s) match")


def test_missing_path_is_an_error_not_a_crash(repo):
    assert run_search(str(repo), "x", path="nope").startswith("ERROR: 'nope' does not exist")


def test_path_escape_is_refused(monkeypatch, repo):
    tools = _tools(monkeypatch, repo)
    out = tools["search_project"].invoke({"repo": "demo", "pattern": "root", "path": "../"})
    assert out.startswith("ERROR")


def test_repeat_guard_answers_the_second_identical_call_from_cache_and_refuses_the_third(monkeypatch, repo):
    tools = _tools(monkeypatch, repo)
    args = {"repo": "demo", "pattern": "accent", "path": "frontend"}
    first = tools["search_project"].invoke(args)
    assert first.startswith("2 hit(s)")
    second = tools["search_project"].invoke(args)
    assert second.startswith("IDENTICAL to a search you already ran")
    third = tools["search_project"].invoke(args)
    assert third.startswith("ERROR: you have run this exact search 3 times")
    # a different search is still fine
    assert tools["search_project"].invoke({"repo": "demo", "pattern": "--bg", "fixed": True}).startswith("1 hit(s)")


def test_search_budget_ends_the_searching(monkeypatch, repo):
    monkeypatch.setattr(rs, "_values", {**rs._values, "planning_search_budget": 3.0})
    tools = _tools(monkeypatch, repo)
    for i in range(3):
        assert not tools["search_project"].invoke({"repo": "demo", "pattern": f"accent{i}"}).startswith("ERROR")
    out = tools["find_files"].invoke({"repo": "demo", "glob": "*.css"})
    assert out.startswith("ERROR: this turn's search budget (3 searches) is spent")
    assert "save_plan" in out


def test_planner_offers_search_and_find(monkeypatch, repo):
    names = set(_tools(monkeypatch, repo))
    assert {"search_project", "find_files", "read_project_file", "list_project_dir", "save_brief", "save_plan"} <= names
