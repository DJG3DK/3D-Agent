"""Covers the edit-repeat guard: a model can resubmit the exact same
(path, old_string, new_string) edit call several times in a row, each time
getting a clear diagnostic error and ignoring it. A plain in-closure guard is
insufficient on its own, because make_agent_tools() is called fresh on every
single work_node pass (every verify_and_ship loop-back), so a guard scoped
only to one call's closure resets to empty before the model's next identical
attempt in the next pass. The real fix round-trips the guard's state through
a parameter (initial_last_failed_edit) and a return value, so the caller
(work_node) can persist it in AgentState across passes -- these tests cover
both the single-pass behavior and the cross-pass persistence.
"""

from agent.tools.agent_tools import make_agent_tools


def _write(tmp_path, rel_path: str, content: str) -> None:
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _tools(tmp_path, initial_last_failed_edit=None):
    tools, last_failed_edit_ref = make_agent_tools(str(tmp_path), initial_last_failed_edit=initial_last_failed_edit)
    by_name = {t.name: t for t in tools}
    return by_name["read"], by_name["write"], by_name["edit"], last_failed_edit_ref


def test_first_failure_gets_the_real_str_replace_error(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    _, _, edit, _ = _tools(tmp_path)
    result = edit.invoke({"path": "a.txt", "old_string": "nope not here", "new_string": "x"})
    assert result.startswith("ERROR:")
    assert "REFUSED" not in result


def test_identical_repeat_within_one_pass_is_refused_without_retrying(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    _, _, edit, _ = _tools(tmp_path)
    args = {"path": "a.txt", "old_string": "nope not here", "new_string": "x"}
    first = edit.invoke(args)
    second = edit.invoke(args)
    assert "REFUSED" in second
    assert first != second, "the refusal message must be visibly distinct from str_replace's own error"


def test_a_different_old_string_after_a_failure_is_not_refused(tmp_path):
    """The guard must be scoped to the EXACT failed signature, not "any edit
    to this path after a failure" -- a genuinely corrected attempt (the
    intended recovery path) must go through normally."""
    _write(tmp_path, "a.txt", "hello world\n")
    _, _, edit, _ = _tools(tmp_path)
    first = edit.invoke({"path": "a.txt", "old_string": "nope not here", "new_string": "x"})
    assert first.startswith("ERROR:")
    second = edit.invoke({"path": "a.txt", "old_string": "hello", "new_string": "goodbye"})
    assert second == "OK"


def test_successful_edit_clears_the_guard(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    _, write, edit, ref = _tools(tmp_path)
    args = {"path": "a.txt", "old_string": "hello", "new_string": "hi"}
    edit.invoke(args)
    assert ref.get("signature") is None
    write.invoke({"path": "a.txt", "content": "hello world\n"})
    result = edit.invoke(args)  # same signature as the FIRST successful call -- must work, not be refused
    assert result == "OK"


def test_write_to_the_path_clears_the_guard(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    _, write, edit, _ = _tools(tmp_path)
    bad_args = {"path": "a.txt", "old_string": "nope not here", "new_string": "x"}
    edit.invoke(bad_args)  # fails, sets guard
    write.invoke({"path": "a.txt", "content": "nope not here now it is\n"})  # content changed
    result = edit.invoke(bad_args)  # same signature, but file changed since -- must actually retry
    assert result == "OK"


def test_refusal_never_writes_the_file(tmp_path):
    original = "hello world\n"
    _write(tmp_path, "a.txt", original)
    _, _, edit, _ = _tools(tmp_path)
    args = {"path": "a.txt", "old_string": "nope not here", "new_string": "x"}
    edit.invoke(args)
    edit.invoke(args)  # the refused one
    assert (tmp_path / "a.txt").read_text() == original


def test_cross_pass_repeat_across_separate_make_agent_tools_calls_is_caught(tmp_path):
    """The real-world failure shape: each work pass calls make_agent_tools()
    fresh (a brand new closure), and the model retries the identical edit
    once per pass across several separate passes -- never twice within the
    same closure instance, so an in-closure-only guard never fires.
    Simulates this by calling make_agent_tools() again for "pass 2", seeded
    with pass 1's final guard state -- exactly what work_node does via
    AgentState's last_failed_edit_signature field."""
    _write(tmp_path, "a.txt", "hello world\n")
    args = {"path": "a.txt", "old_string": "nope not here", "new_string": "x"}

    # Pass 1: a brand new agent/tools instance, ONE attempt, then the pass ends.
    _, _, edit_pass1, ref_pass1 = _tools(tmp_path)
    result_pass1 = edit_pass1.invoke(args)
    assert result_pass1.startswith("ERROR:")
    assert "REFUSED" not in result_pass1
    persisted_signature = ref_pass1.get("signature")
    assert persisted_signature is not None

    # Pass 2: a COMPLETELY FRESH make_agent_tools() call (as work_node does
    # on every pass), seeded with what pass 1 persisted -- the model retries
    # the SAME edit, believing it's a first attempt in a fresh context.
    _, _, edit_pass2, _ = _tools(tmp_path, initial_last_failed_edit=persisted_signature)
    result_pass2 = edit_pass2.invoke(args)
    assert "REFUSED" in result_pass2, "cross-pass repeat must be caught"


def test_a_fresh_pass_with_no_prior_state_is_not_refused(tmp_path):
    """Sanity check: initial_last_failed_edit=None (a task's first pass, or
    one that never failed) must behave exactly like the no-argument case."""
    _write(tmp_path, "a.txt", "hello world\n")
    _, _, edit, _ = _tools(tmp_path, initial_last_failed_edit=None)
    result = edit.invoke({"path": "a.txt", "old_string": "nope not here", "new_string": "x"})
    assert "REFUSED" not in result


# ---------------------------------------------------------------------------
# /workspace path normalization: a model calling read/write with
# '/workspace/apps/...' is following the prompt's own framing (bash genuinely
# shows the repo there), so that spelling must be accepted directly rather
# than rejected with a path-escape error that contradicts what the agent was
# told.
# ---------------------------------------------------------------------------


async def test_read_accepts_workspace_prefixed_path(tmp_path):
    _write(tmp_path, "apps/x.txt", "the content\n")
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "/workspace/apps/x.txt"})
    assert "the content" in result
    assert "ERROR" not in result


async def test_write_accepts_workspace_prefixed_path(tmp_path):
    _, write, _, _ = _tools(tmp_path)
    result = write.invoke({"path": "/workspace/newdir/new.txt", "content": "written\n"})
    assert result == "OK"
    assert (tmp_path / "newdir" / "new.txt").read_text() == "written\n"


def test_edit_accepts_workspace_prefixed_path(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    _, _, edit, _ = _tools(tmp_path)
    result = edit.invoke({"path": "/workspace/a.txt", "old_string": "hello", "new_string": "goodbye"})
    assert result == "OK"
    assert (tmp_path / "a.txt").read_text() == "goodbye world\n"


async def test_other_absolute_paths_still_rejected_without_leaking_host_path(tmp_path):
    """/workspace/ is the ONLY absolute spelling accepted -- an arbitrary
    host path must still be rejected, and the error must instruct on the
    relative-path convention rather than naming the host sandbox root
    (which contradicts the /workspace story the agent operates under)."""
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "/etc/passwd"})
    assert result.startswith("ERROR")
    assert str(tmp_path) not in result
    assert "RELATIVE to the repo root" in result


# ---------------------------------------------------------------------------
# read paging: on a too-large result, a model's instinct is to call `read`
# again with offset/limit -- without direct paging support, each retry would
# re-offload the whole file to a fresh pointer and loop. `read` pages directly.
# ---------------------------------------------------------------------------


async def test_read_pages_with_offset_and_limit(tmp_path):
    _write(tmp_path, "big.txt", "\n".join(f"line {i}" for i in range(1, 501)) + "\n")
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "big.txt", "offset": 100, "limit": 3})
    assert "100\tline 100" in result
    assert "102\tline 102" in result
    assert "line 103" not in result
    assert "of 501" in result  # tells the model the real extent


async def test_whole_source_file_under_inline_cap_returns_in_full(tmp_path):
    """Regression for a read/summarize loop: a ~2,000-line file (over the
    offload cap, under the source-file inline cap) must come back whole in
    one call -- paging it in small windows is what caused the loop."""
    content = ("const x = 1;" + " " * 20 + "\n") * 2000   # ~66K chars
    _write(tmp_path, "big_source.tsx", content)
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "big_source.tsx"})
    assert result == content          # verbatim, no preview, no pointer
    assert "offset" not in result


async def test_read_large_file_returns_pageable_preview_not_pointer(tmp_path):
    _write(tmp_path, "huge.txt", ("x" * 80 + "\n") * 2600)  # > 200K inline cap
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "huge.txt"})
    assert "offset" in result and "limit" in result  # instructs paging via THIS tool
    assert "/tool_outputs/" not in result  # no more pointer-file indirection for reads


async def test_read_offset_past_end_reports_line_count(tmp_path):
    _write(tmp_path, "small.txt", "one\ntwo\n")
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "small.txt", "offset": 999, "limit": 5})
    assert "no lines at offset 999" in result


async def test_read_refuses_binary_file_instead_of_dumping_raw_bytes(tmp_path):
    """A binary file (e.g. an uploaded image) read via this tool used to
    decode as garbage text with no size guard, since the size caps here are
    tuned for legitimate large source files -- real image bytes routinely
    land under them. That garbage then sat in conversation history forever,
    making every later call in the same thread more expensive. Must refuse
    outright and point to describe_image instead."""
    p = tmp_path / "image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\xff" * 500)
    read, _, _, _ = _tools(tmp_path)
    result = await read.ainvoke({"path": "image.png"})
    assert result.startswith("ERROR:")
    assert "describe_image" in result
