"""`read` accepts the spelling the model actually reaches for.

The built-in memory tools take `file_path`; these repo tools take `path`. The
system prompt spells out the difference twice, and on 2026-09-12 a coder still
called `read` with `file_path` -- four times, on
".codeql-verify/cb/codeql/qlpacks/.../AlertSuppression.qll".

A wrong argument NAME is worse than a wrong value: it fails schema validation
before the tool body runs, so there is no message from inside the tool telling
the model what to fix. Watching the stream, the operator saw what it did
instead -- fall back to `cat` in bash, the exact habit the rest of the harness
spends its effort discouraging.

So both spellings work now. These tests cover that, and that a call with
neither gets a usable error rather than a crash -- both spellings had to become
optional in the schema for either alone to validate.
"""

from __future__ import annotations

import asyncio

from agent.tools.agent_tools import make_agent_tools


def _tools(tmp_path):
    tools, _ = make_agent_tools(str(tmp_path))
    return {t.name: t for t in tools}


def _invoke(tool, **kwargs):
    out = tool.invoke(kwargs) if not tool.coroutine else asyncio.run(tool.ainvoke(kwargs))
    return out


def test_read_accepts_file_path(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tools = _tools(tmp_path)
    assert "hello" in _invoke(tools["read"], file_path="a.txt")
    assert "hello" in _invoke(tools["read"], path="a.txt"), "the canonical name still works"


def test_the_failure_from_the_live_run_now_succeeds(tmp_path):
    """The real call, with the real path shape."""
    target = tmp_path / ".codeql-verify" / "cb" / "codeql" / "suppression"
    target.mkdir(parents=True)
    (target / "AlertSuppression.qll").write_text("class Suppression { }\n")
    tools = _tools(tmp_path)
    out = _invoke(tools["read"], file_path=".codeql-verify/cb/codeql/suppression/AlertSuppression.qll")
    assert "class Suppression" in out


def test_write_and_edit_accept_file_path(tmp_path):
    tools = _tools(tmp_path)
    assert _invoke(tools["write"], file_path="b.txt", content="one\n") == "OK"
    assert (tmp_path / "b.txt").read_text() == "one\n"
    assert _invoke(tools["edit"], file_path="b.txt", old_string="one", new_string="two") == "OK"
    assert (tmp_path / "b.txt").read_text() == "two\n"


def test_paging_still_works_through_the_alias(tmp_path):
    (tmp_path / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 400)))
    tools = _tools(tmp_path)
    out = _invoke(tools["read"], file_path="big.txt", offset=200, limit=3)
    assert "line 200" in out and "line 202" in out
    assert "line 1\n" not in out


def test_no_path_at_all_is_a_clear_error_not_a_crash(tmp_path):
    tools = _tools(tmp_path)
    for name in ("read", "write", "edit"):
        out = _invoke(tools[name], **({"content": "x"} if name == "write" else {}))
        assert "no path given" in out, f"{name}: {out[:80]}"


def test_the_edit_repeat_guard_keys_on_the_resolved_path(tmp_path):
    """The guard's signature is (path, old_string, new_string). If the alias
    resolved after the signature was computed, the same failed edit submitted
    under each spelling would slip past it."""
    (tmp_path / "c.txt").write_text("hello\n")
    tools = _tools(tmp_path)
    first = _invoke(tools["edit"], path="c.txt", old_string="nope", new_string="x")
    assert "ERROR" in first and "REFUSED" not in first
    second = _invoke(tools["edit"], file_path="c.txt", old_string="nope", new_string="x")
    assert "REFUSED" in second, "the same edit under the other spelling must still be caught"


def test_an_edit_with_no_old_string_says_so(tmp_path):
    """Accepting either path spelling made every argument optional in the
    schema, so this call now reaches the body instead of being rejected by
    validation. It used to come back "old_string is not unique (13
    occurrences)" -- true of the empty string, and no help at all."""
    (tmp_path / "a.txt").write_text("hello world\n")
    tools = _tools(tmp_path)
    out = _invoke(tools["edit"], path="a.txt")
    assert "no old_string given" in out
    assert (tmp_path / "a.txt").read_text() == "hello world\n", "and it changed nothing"


def test_a_forgotten_old_string_does_not_poison_the_repeat_guard(tmp_path):
    """The empty-args call must not be recorded as a failed edit: the model's
    corrected retry would then look like a resubmission."""
    (tmp_path / "a.txt").write_text("hello world\n")
    tools = _tools(tmp_path)
    _invoke(tools["edit"], path="a.txt")
    out = _invoke(tools["edit"], path="a.txt", old_string="hello", new_string="goodbye")
    assert out == "OK", out
    assert (tmp_path / "a.txt").read_text() == "goodbye world\n"


def test_write_with_no_content_still_creates_an_empty_file(tmp_path):
    """Unlike edit, an empty `content` is a real request."""
    tools = _tools(tmp_path)
    assert _invoke(tools["write"], path="empty.txt") == "OK"
    assert (tmp_path / "empty.txt").read_text() == ""
