"""The map and the runbooks have to stay true, or they are worse than nothing.

These check the handful of facts that a reader will act on and that change
silently: the ports, the health routes, the secret files, the graph's shape.
Prose is not tested -- claims a reader would follow are.
"""
import pathlib
import re

import pytest

DOCS = pathlib.Path("docs")
ARCH = DOCS / "architecture.md"


def test_the_map_and_runbooks_exist():
    assert ARCH.exists()
    for page in ("README.md", "stuck-task.md", "consolidation.md",
                 "router-refusals.md", "merge-vs-github.md"):
        assert (DOCS / "runbooks" / page).exists(), page
    assert (DOCS / "backup.md").exists()


def test_every_internal_link_resolves():
    bad = []
    pages = list(DOCS.rglob("*.md")) + [pathlib.Path(p) for p in ("README.md", "INSTALL.md", "CONTRIBUTING.md")]
    for md in pages:
        for m in re.finditer(r"\[([^\]]+)\]\(([^)#][^)]*)\)", md.read_text()):
            target = m.group(2).split("#")[0]
            if target.startswith(("http", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                bad.append(f"{md} -> {target}")
    assert not bad, f"broken documentation links: {bad}"


def test_the_map_names_the_ports_the_code_actually_uses():
    """A wrong port sends a reader to the wrong process at the worst moment."""
    text = ARCH.read_text()
    for port in ("8100", "4000", "4100", "4101"):
        assert port in text, f"architecture.md does not mention port {port}"
    assert "4101" in pathlib.Path("services/commit-reviewer/reviewer.js").read_text()
    assert "4100" in pathlib.Path("services/agent-review/server.js").read_text()


def test_the_health_routes_the_docs_promise_exist_in_the_code():
    assert '@app.get("/api/health")' in pathlib.Path("agent/server.py").read_text()
    assert "app.get('/health'" in pathlib.Path("services/agent-review/server.js").read_text()
    assert "'/health'" in pathlib.Path("services/commit-reviewer/reviewer.js").read_text()


def test_the_map_points_at_the_real_secret_files():
    """The review secret moved out of the router's .env; the map must say so,
    and both services must really read it from the shared module."""
    text = ARCH.read_text()
    assert "services/shared/.env" in text
    for service in ("services/agent-review/server.js", "services/commit-reviewer/reviewer.js"):
        assert "readServiceSecret" in pathlib.Path(service).read_text(), service


def test_the_graph_really_is_the_two_nodes_the_map_describes():
    graph = pathlib.Path("agent/outer_graph.py").read_text()
    nodes = set(re.findall(r'add_node\(\s*"([a-z_]+)"', graph))
    assert nodes == {"work", "verify_and_ship"}, f"the map says two nodes; the graph has {nodes}"
    assert "work" in ARCH.read_text() and "verify_and_ship" in ARCH.read_text()


@pytest.mark.parametrize("script", ["scripts/backup.sh", "scripts/verify_backup_restore.sh"])
def test_the_backup_scripts_exist_and_are_executable(script):
    p = pathlib.Path(script)
    assert p.exists(), script
    assert p.stat().st_mode & 0o111, f"{script} is not executable"
    assert script in pathlib.Path("docs/backup.md").read_text()
