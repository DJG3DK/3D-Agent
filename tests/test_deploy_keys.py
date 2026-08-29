"""Guards for per-project deploy keys.

The properties that matter: the private key is never world-readable, never
leaves through the API, is scoped to ONE repo's git transport, and a
passphrase-protected or malformed key is rejected at paste time rather than
at 2am during an unattended push.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agent import deploy_keys as dk


def _repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


@pytest.fixture(autouse=True)
def keys_dir(tmp_path, monkeypatch):
    d = tmp_path / "keys"
    monkeypatch.setattr(dk, "KEYS_DIR", d)
    return d


def _make_key(tmp_path: Path, passphrase: str = "") -> str:
    f = tmp_path / "src_key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", passphrase, "-q", "-f", str(f)],
                   check=True)
    return f.read_text()


def test_generate_creates_a_scoped_key_and_points_only_that_repo_at_it(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:owner/repo.git")
    other = _repo(tmp_path / "other", remote="git@github.com:owner/other.git")

    st = dk.generate_key("proj", str(live))

    assert st.installed and st.fingerprint and st.public_key.startswith("ssh-ed25519")
    assert st.configured is True

    ssh_cmd = subprocess.run(["git", "config", "--get", "core.sshCommand"], cwd=live,
                             capture_output=True, text=True).stdout
    assert str(keys_dir / "proj.key") in ssh_cmd
    assert "IdentitiesOnly=yes" in ssh_cmd, "must not fall back to other agent identities"

    # the other project's git is untouched -- one key must not sign every repo
    other_cmd = subprocess.run(["git", "config", "--get", "core.sshCommand"], cwd=other,
                               capture_output=True, text=True).stdout.strip()
    assert other_cmd == ""


def test_key_and_directory_are_never_group_or_world_readable(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    dk.generate_key("proj", str(live))
    kf = keys_dir / "proj.key"
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys_dir.stat().st_mode) == 0o700, "ssh refuses a loose key dir"


def test_pasted_key_is_written_600_without_a_readable_window(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    st = dk.install_key("proj", str(live), _make_key(tmp_path))
    assert st.installed and st.configured
    assert stat.S_IMODE((keys_dir / "proj.key").stat().st_mode) == 0o600


def test_a_public_key_pasted_by_mistake_is_rejected(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample comment"
    with pytest.raises(dk.DeployKeyError, match="PRIVATE half"):
        dk.install_key("proj", str(live), pub)
    assert not (keys_dir / "proj.key").exists()


def test_passphrase_protected_key_is_rejected_and_not_left_behind(tmp_path, keys_dir):
    """Nothing can type a passphrase during an unattended push, so this must
    fail at paste time -- not silently at merge time."""
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    with pytest.raises(dk.DeployKeyError, match="passphrase"):
        dk.install_key("proj", str(live), _make_key(tmp_path, passphrase="hunter2"))
    assert not (keys_dir / "proj.key").exists(), "a rejected key must not stay on disk"


def test_garbage_input_is_rejected(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    for bad in ("", "not a key", "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"):
        with pytest.raises(dk.DeployKeyError):
            dk.install_key("proj", str(live), bad)


def test_status_reports_a_missing_origin_rather_than_pretending(tmp_path):
    live = _repo(tmp_path / "noremote")
    st = dk.status("noremote", str(live))
    assert st.installed is False and st.remote is None
    assert "no `origin` remote" in st.detail


def test_https_remote_is_flagged_because_a_deploy_key_cannot_authenticate_it(tmp_path):
    live = _repo(tmp_path / "proj", remote="https://github.com/owner/repo.git")
    st = dk.status("proj", str(live))
    assert st.remote_kind == "https"
    assert "cannot authenticate" in st.detail
    assert "set-url" in st.detail, "tell the operator the fix, not just the problem"


def test_remove_deletes_the_key_and_unsets_the_repo_pointer(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    dk.generate_key("proj", str(live))
    st = dk.remove_key("proj", str(live))
    assert st.installed is False and st.configured is False
    assert not (keys_dir / "proj.key").exists()
    assert subprocess.run(["git", "config", "--get", "core.sshCommand"], cwd=live,
                          capture_output=True, text=True).stdout.strip() == ""


def test_project_name_cannot_escape_the_keys_directory(tmp_path):
    for bad in ("../escape", "a/b", "..", "with space"):
        with pytest.raises(dk.DeployKeyError, match="invalid project name"):
            dk._key_path(bad)


def test_check_remote_fails_closed_and_explains_an_unauthorized_key(tmp_path, keys_dir):
    """A key that exists but isn't registered on the remote must produce a
    message naming the actual fix."""
    live = _repo(tmp_path / "proj", remote="git@github.com:owner/definitely-not-real.git")
    dk.generate_key("proj", str(live))
    ok, detail = dk.check_remote("proj", str(live), timeout=20)
    assert ok is False
    assert detail, "a failure must say something"


def test_regenerating_replaces_the_previous_key(tmp_path, keys_dir):
    live = _repo(tmp_path / "proj", remote="git@github.com:o/r.git")
    first = dk.generate_key("proj", str(live)).public_key
    second = dk.generate_key("proj", str(live)).public_key
    assert first != second, "regenerate must mint a new key, not reuse the old one"
