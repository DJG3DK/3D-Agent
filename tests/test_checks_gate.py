"""audit H-17: run_check must distinguish a missing/unreadable package.json
from a present manifest with no such script. The old `cat ... || true` made
both look identical (empty output -> "skipped"), so the gate reported a green
run it never performed when the worktree was missing or broken."""
import json

import pytest

from agent.tools import checks


@pytest.mark.asyncio
async def test_missing_package_json_is_a_failed_check_not_a_skip(tmp_path):
    # empty dir, no package.json
    result = await checks.run_check(str(tmp_path), "test:review", timeout=10)
    assert result["ran"] is False
    assert result["ok"] is False, "a missing manifest must FAIL, never silently pass"
    assert "package.json not found" in result["output"]


@pytest.mark.asyncio
async def test_present_manifest_without_script_is_a_real_skip(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}))
    result = await checks.run_check(str(tmp_path), "test:review", timeout=10)
    assert result["ran"] is False
    assert result["ok"] is True, "no such script, but the manifest is fine -> legit skip"
    assert "skipped" in result["output"]
