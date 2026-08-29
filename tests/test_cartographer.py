"""Cartographer: inventory determinism, hash stability, and the skip marker.

These are the properties that make scheduling it cheap. If the hash moves when
nothing structural changed, every run calls the model and the marker is
pointless; if it does NOT move when structure changes, maps silently go stale.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.cartographer import build_inventory, inventory_hash


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    (tmp_path / "src" / "util.py").write_text("def f(): pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_x(): pass\n")
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo", "scripts": {"test": "pytest"}}))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x" * 5000)
    return tmp_path


def test_inventory_skips_dependency_dirs(tmp_path):
    inv = build_inventory("demo", str(_repo(tmp_path)))
    assert not any("node_modules" in p for p in inv["tree"]), \
        "node_modules must never enter the inventory — it would dominate the language histogram"
    assert inv["languages"].get("Python") == 3


def test_inventory_finds_tests_and_manifests(tmp_path):
    inv = build_inventory("demo", str(_repo(tmp_path)))
    assert any("test_main.py" in t for t in inv["test_files"])
    assert "package.json" in inv["manifests"]
    assert "pytest" in inv["manifests"]["package.json"]


def test_hash_is_stable_across_runs(tmp_path):
    root = str(_repo(tmp_path))
    assert inventory_hash(build_inventory("demo", root)) == inventory_hash(build_inventory("demo", root))


def test_hash_moves_when_structure_changes(tmp_path):
    root = _repo(tmp_path)
    before = inventory_hash(build_inventory("demo", str(root)))
    (root / "src" / "new_module.py").write_text("x = 1\n")
    after = inventory_hash(build_inventory("demo", str(root)))
    assert before != after, "a new source file must invalidate the map"


def test_hash_ignores_commit_churn(tmp_path):
    """recent_commits and hot_files change on every commit. If they fed the
    hash, every commit would trigger a full re-map — the marker would save
    nothing."""
    root = str(_repo(tmp_path))
    inv = build_inventory("demo", root)
    base = inventory_hash(inv)
    inv2 = dict(inv)
    inv2["recent_commits"] = "totally different subjects"
    inv2["hot_files"] = {"src/main.py": 99}
    assert inventory_hash(inv2) == base
