"""The deploy-key HTTP surface.

The property this file is really guarding: the private key goes IN and never
comes back out. Everything else here is access control and error shape.
"""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import config as agent_config
from agent import deploy_keys as dk
from agent.auth import User

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)
_USER = User(id=2, email="dev@example.com", role="user", allowed_repos=["proj"],
             totp_enabled=True, must_change_password=False,
             auto_approve_commands=False, require_merge_review=True)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    live = tmp_path / "proj"
    live.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=live, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:owner/proj.git"],
                   cwd=live, check=True)
    monkeypatch.setattr(dk, "KEYS_DIR", tmp_path / "keys")
    monkeypatch.setattr(agent_config, "PROJECTS",
                        {"proj": {"live": str(live), "sandbox": str(tmp_path / "ws")}},
                        raising=False)
    monkeypatch.setattr(srv, "PROJECTS", agent_config.PROJECTS, raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    return {"live": live, "keys": tmp_path / "keys"}


def test_generate_returns_the_public_half_and_never_the_private_one(wired):
    client = TestClient(srv.app)
    res = client.post("/api/projects/proj/deploy-key/generate")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["installed"] is True and body["configured"] is True
    assert body["public_key"].startswith("ssh-ed25519")
    assert body["fingerprint"]

    private = (wired["keys"] / "proj.key").read_text()
    assert "PRIVATE KEY" in private, "the key really was written"
    blob = json.dumps(body)
    assert "PRIVATE KEY" not in blob, "the private half must never cross the API"
    # and the read endpoint must not leak it either
    got = client.get("/api/projects/proj/deploy-key").json()
    assert "PRIVATE KEY" not in json.dumps(got)


def test_install_accepts_a_pasted_key_and_rejects_a_public_one(wired, tmp_path):
    src = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(src)], check=True)
    client = TestClient(srv.app)

    res = client.post("/api/projects/proj/deploy-key",
                      json={"private_key": src.read_text()})
    assert res.status_code == 200, res.text
    assert res.json()["installed"] is True

    res = client.post("/api/projects/proj/deploy-key",
                      json={"private_key": (src.with_suffix(".pub")).read_text()})
    assert res.status_code == 400
    assert "PRIVATE half" in res.json()["detail"]


def test_status_for_a_project_with_no_key(wired):
    body = TestClient(srv.app).get("/api/projects/proj/deploy-key").json()
    assert body["installed"] is False
    assert body["remote"] == "git@github.com:owner/proj.git"
    assert body["remote_kind"] == "ssh"


def test_delete_removes_it(wired):
    client = TestClient(srv.app)
    client.post("/api/projects/proj/deploy-key/generate")
    body = client.delete("/api/projects/proj/deploy-key").json()
    assert body["installed"] is False and body["configured"] is False
    assert not (wired["keys"] / "proj.key").exists()


def test_unknown_project_is_404_not_a_key_write(wired):
    res = TestClient(srv.app).post("/api/projects/nope/deploy-key/generate")
    assert res.status_code == 404
    assert not (wired["keys"] / "nope.key").exists()


def test_every_endpoint_is_admin_only(wired, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _USER)
    client = TestClient(srv.app)
    for method, path, payload in (
        ("get", "/api/projects/proj/deploy-key", None),
        ("post", "/api/projects/proj/deploy-key", {"private_key": "x"}),
        ("post", "/api/projects/proj/deploy-key/generate", None),
        ("post", "/api/projects/proj/deploy-key/test", None),
        ("delete", "/api/projects/proj/deploy-key", None),
    ):
        res = getattr(client, method)(path, **({"json": payload} if payload else {}))
        assert res.status_code in (401, 403), f"{method} {path} allowed a non-admin"
    assert not (wired["keys"] / "proj.key").exists()
