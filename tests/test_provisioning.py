"""Detection + provisioning guards for the project onboarding wizard.

The highest-value test in this file is
test_test_script_hitting_a_live_service_is_flagged_and_disabled: it encodes
the incident this deployment already lived through -- a test suite that
POSTed real trade orders at a live bot -- so onboarding can never silently
enable that class of script again.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent import provisioning as prov


@pytest.fixture(autouse=True)
def _allow_tmp_as_project_root(monkeypatch, tmp_path):
    """Detection enforces AGENT_PROJECT_ROOTS containment (see
    test_onboarding_security.py). These unit tests build their fixtures under
    tmp_path, so allow it explicitly rather than weakening the default."""
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path / "workspaces"))


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


@pytest.fixture
def node_repo(tmp_path):
    repo = tmp_path / "shop-api"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "shop-api",
        "scripts": {
            "typecheck": "tsc --noEmit",
            "lint": "eslint src",
            "build": "vite build",
            "test": "vitest run",
        },
    }))
    (repo / "package-lock.json").write_text("{}")
    (repo / ".gitignore").write_text(".env\nconfig/keys.json\ndata/fixtures\nnode_modules\n")
    (repo / ".env").write_text("DATABASE_URL=postgres://localhost/x\n")
    (repo / "config").mkdir()
    (repo / "config" / "keys.json").write_text("{}")
    (repo / "data" / "fixtures").mkdir(parents=True)
    (repo / "data" / "fixtures" / "sample.json").write_text("[]")
    _git_init(repo)
    return repo


def test_detects_stack_scripts_and_build_steps(node_repo):
    r = prov.detect_project(str(node_repo))
    assert r.blockers == []
    assert r.is_git_repo and r.package_manager == "npm"
    assert "node" in r.languages
    names = [c["name"] for c in r.checks]
    assert names == ["typecheck", "lint", "build", "test"]
    assert all(c["cmd"] == "npm" for c in r.checks)
    # install always precedes build in the deploy steps
    assert r.build_steps[0]["args"][0] == "install"
    assert r.build_steps[-1]["args"] == ["run", "build"]


def test_gitignored_credentials_are_proposed_and_fixtures_default_off(node_repo):
    r = prov.detect_project(str(node_repo))
    secrets = {c.value: c for c in r.secret_files}
    assert ".env" in secrets and "config/keys.json" in secrets
    assert all(c.enabled for c in r.secret_files), "secrets are needed for checks to be real"
    mounts = {c.value: c for c in r.read_only_mounts}
    assert "data/fixtures" in mounts
    assert mounts["data/fixtures"].enabled is False, "mounting host dirs must be opt-in"
    # a gitignored env carrying DATABASE_URL wires the project_db tool
    assert r.db_env_file == ".env"


def test_test_script_hitting_a_live_service_is_flagged_and_disabled(tmp_path):
    """The my-service lesson: test:routes POSTed real orders at a live service."""
    repo = tmp_path / "trader"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_routes.js").write_text(
        "await fetch('http://a live service/trade/open', {method:'POST'});\n")
    (repo / "tests" / "test_pure.js").write_text("assert(1+1===2);\n")
    (repo / "package.json").write_text(json.dumps({
        "scripts": {
            "test:routes": "node tests/test_routes.js",
            "test:pure": "node tests/test_pure.js",
            "test": "npm run test:routes && npm run test:pure",
        },
    }))
    (repo / "package-lock.json").write_text("{}")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    flagged = {c.value: c for c in r.risky_scripts}
    assert "test:routes" in flagged, "a test making live HTTP calls must be flagged"
    assert "test:pure" not in flagged, "pure logic tests must not be flagged"
    assert flagged["test:routes"].enabled is False
    assert flagged["test:routes"].warning
    # with no curated safe list, the aggregate `test` script must NOT be
    # auto-enabled -- it chains the dangerous one.
    assert "test" not in [c["name"] for c in r.checks]
    assert any("network calls" in w for w in r.warnings)


def test_repo_declaring_test_review_is_trusted_over_the_aggregate(tmp_path):
    repo = tmp_path / "curated"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "live.js").write_text("fetch('http://127.0.0.1:9/x')\n")
    (repo / "package.json").write_text(json.dumps({
        "scripts": {"test:live": "node tests/live.js",
                    "test:review": "node tests/pure.js",
                    "test": "npm run test:live"},
    }))
    (repo / "package-lock.json").write_text("{}")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    test_check = next(c for c in r.checks if c["name"] == "test")
    assert test_check["args"] == ["run", "test:review"], "the repo's own safe list wins"


def test_monorepo_workspaces_expand_to_node_modules_dirs_and_builds(tmp_path):
    repo = tmp_path / "mono"
    (repo / "apps" / "api").mkdir(parents=True)
    (repo / "apps" / "web").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({
        "workspaces": ["apps/*"], "scripts": {"build": "turbo build"}}))
    (repo / "pnpm-lock.yaml").write_text("")
    (repo / "apps" / "api" / "package.json").write_text(json.dumps({"scripts": {"build": "nest build"}}))
    (repo / "apps" / "web" / "package.json").write_text(json.dumps({"scripts": {}}))
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.package_manager == "pnpm"
    assert r.node_modules_dirs == [".", "apps/api", "apps/web"]
    assert r.build_steps[0]["args"] == ["install", "--frozen-lockfile"]
    build_dirs = [s["dir"] for s in r.build_steps if s["args"][:1] == ["run"]]
    assert build_dirs == [".", "apps/api"], "only packages with a build script"


def test_python_project_gets_pytest_checks(tmp_path):
    repo = tmp_path / "pyproj"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "python" in r.languages
    assert [c["name"] for c in r.checks] == ["test"]
    assert r.checks[0]["args"] == ["-m", "pytest", "-q"]


def test_non_git_directory_is_a_blocker(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = prov.detect_project(str(plain))
    assert any("not a git repository" in b for b in r.blockers)


def test_duplicate_name_is_a_blocker(node_repo):
    r = prov.detect_project(str(node_repo), existing_names=["shop-api"])
    assert any("already configured" in b for b in r.blockers)


def test_relative_path_is_rejected():
    with pytest.raises(prov.ProvisioningError):
        prov.detect_project("./somewhere")


def test_project_with_no_manifest_warns_but_does_not_block(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.blockers == []
    assert any("no recognized project manifest" in w for w in r.warnings)
    assert any("no automated checks" in w for w in r.warnings)


# --- provisioning ---------------------------------------------------------

def test_config_from_choices_uses_operator_answers_not_detection(node_repo):
    """An operator's rejection must survive: detection proposed .env AND
    config/keys.json; the operator kept only .env."""
    entry = prov.config_from_choices(
        "shop-api", str(node_repo), "/tmp/wt/shop-api",
        {"secret_files": [".env"], "checks": [{"name": "lint", "dir": ".", "cmd": "npm",
                                               "args": ["run", "lint"]}],
         "pm2_apps": ["shop"], "build_steps": [{"dir": ".", "cmd": "npm", "args": ["ci"]}],
         "node_modules_dirs": ["."], "db_env_file": ".env"},
    )
    assert entry["review"]["secretFiles"] == [".env"]
    assert entry["deploy"]["pm2Apps"] == ["shop"]
    assert entry["db_env_file"] == ".env"
    assert "readOnlyMounts" not in entry["review"], "empty choices stay absent, not empty lists"


def test_write_project_entry_is_atomic_and_refuses_duplicates(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": {"a": {"live": "/a", "sandbox": "/s/a"}}}))
    prov.write_project_entry(p, "b", {"live": "/b", "sandbox": "/s/b"})
    data = json.loads(p.read_text())
    assert set(data["projects"]) == {"a", "b"}
    assert not list(tmp_path.glob("*.tmp")), "temp file must be renamed away"
    with pytest.raises(prov.ProvisioningError):
        prov.write_project_entry(p, "b", {"live": "/b", "sandbox": "/s/b"})


def test_create_worktree_makes_a_real_worktree(node_repo, tmp_path):
    sandbox = tmp_path / "workspaces" / "shop-api"
    ok, out = prov.create_worktree(str(node_repo), str(sandbox))
    assert ok, out
    assert (sandbox / "README.md").is_file()
    # a worktree's .git is a POINTER FILE, not a directory -- the property the
    # sandbox mount logic and tool_errors handling both depend on
    assert (sandbox / ".git").is_file()
    # idempotent: re-running reports the existing worktree rather than failing
    ok2, out2 = prov.create_worktree(str(node_repo), str(sandbox))
    assert ok2 and "already exists" in out2


def test_create_worktree_refuses_to_clobber_a_non_worktree_dir(node_repo, tmp_path):
    sandbox = tmp_path / "occupied"
    sandbox.mkdir()
    (sandbox / "important.txt").write_text("do not delete me")
    ok, out = prov.create_worktree(str(node_repo), str(sandbox))
    assert not ok and "refusing to overwrite" in out
    assert (sandbox / "important.txt").is_file()
