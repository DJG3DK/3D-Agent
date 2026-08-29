"""Per-project SSH deploy keys, so a merged change can reach the git remote.

Where the push actually happens
-------------------------------
The agent never pushes -- `git push` is in its blocked-command list. After a
merge is approved, the REVIEW SERVICE pushes from the project's LIVE checkout
(services/agent-review/server.js). That push uses whatever credentials that
checkout has, and it is deliberately best-effort: no `origin`, or a rejected
push, is reported but never rolls back an already-completed merge.

The consequence for a fresh install: the merge succeeds, the deploy succeeds,
and the remote silently stays behind. This module exists to make that
configurable up front rather than discovered later.

How a key is applied
--------------------
Not by rewriting ~/.ssh/config, and not by a global key -- both would leak one
project's credentials into another's git operations. Instead the private key is
written to a 0600 file and the LIVE repo is told to use it for its own
transport only:

    git -C <live> config core.sshCommand "ssh -i <key> -o IdentitiesOnly=yes"

Per-repo config, so each project pushes as itself, and nothing outside that
repo's git commands can reach the key.

Handling of the secret
----------------------
The private key is write-only from the API's point of view: it can be
installed and replaced, and its fingerprint and status can be read, but there
is no endpoint that returns it. Same contract as the credentials panel.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from agent import paths

# Keys live beside the install, never inside the repo tree that gets committed.
KEYS_DIR: Path = Path(os.environ.get("AGENT_KEYS_DIR") or (paths.REPO_ROOT / "keys"))

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (OPENSSH|RSA|EC|DSA|PGP)? ?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----",
    re.DOTALL,
)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DeployKeyError(Exception):
    """Raised for an input the operator must fix."""


@dataclass
class KeyStatus:
    project: str
    installed: bool
    fingerprint: str | None = None
    public_key: str | None = None
    remote: str | None = None          # the project's `origin` URL, if any
    remote_kind: str | None = None     # "ssh" | "https" | None
    configured: bool = False           # is core.sshCommand pointing at our key
    reachable: bool | None = None      # did `git ls-remote` succeed
    detail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _key_path(project: str) -> Path:
    if not _SAFE_NAME.match(project):
        raise DeployKeyError(f"invalid project name: {project!r}")
    path = (KEYS_DIR / f"{project}.key").resolve()
    # Belt and braces: the name pattern already excludes separators, but the
    # file this returns is written with the private key, so confirm rather
    # than assume it landed inside the keys directory.
    if not str(path).startswith(str(KEYS_DIR.resolve()) + os.sep):
        raise DeployKeyError(f"invalid project name: {project!r}")
    return path


def _run(args: list[str], cwd: str | None = None, timeout: int = 30) -> tuple[bool, str]:
    try:
        res = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def _git(live: str, args: list[str], timeout: int = 30) -> tuple[bool, str]:
    return _run(["git", *args], cwd=live, timeout=timeout)


def fingerprint(key_file: Path) -> str | None:
    ok, out = _run(["ssh-keygen", "-lf", str(key_file)])
    return out.split()[1] if ok and len(out.split()) > 1 else None


def public_key_of(key_file: Path) -> str | None:
    ok, out = _run(["ssh-keygen", "-y", "-f", str(key_file)])
    return out.strip() if ok else None


def _remote_kind(url: str) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return "https"
    return "ssh"


def status(project: str, live: str) -> KeyStatus:
    """What this project's push path looks like right now. Read-only."""
    st = KeyStatus(project=project, installed=False)

    ok, remote = _git(live, ["remote", "get-url", "origin"])
    if ok and remote:
        st.remote = remote.strip()
        st.remote_kind = _remote_kind(st.remote)
    else:
        st.detail = ("no `origin` remote -- merges will deploy locally and the push step "
                     "is skipped entirely")
        return st

    kf = _key_path(project)
    if kf.is_file():
        st.installed = True
        st.fingerprint = fingerprint(kf)
        st.public_key = public_key_of(kf)

    ok, ssh_cmd = _git(live, ["config", "--get", "core.sshCommand"])
    st.configured = bool(ok and str(kf) in (ssh_cmd or ""))

    if st.remote_kind == "https":
        st.detail = ("`origin` is an HTTPS URL -- an SSH deploy key cannot authenticate it. "
                     "Switch the remote to SSH (git remote set-url origin git@github.com:owner/repo.git) "
                     "or configure a credential helper on the host.")
    return st


def check_remote(project: str, live: str, timeout: int = 25) -> tuple[bool, str]:
    """Actually contact the remote, the way the review service's push will.

    `git ls-remote` is the honest test: it performs the same authentication a
    push does without writing anything. BatchMode stops a missing key from
    hanging on an interactive passphrase prompt.
    """
    env_ssh = None
    kf = _key_path(project)
    if kf.is_file():
        env_ssh = f"ssh -i {kf} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    env = {**os.environ}
    if env_ssh:
        env["GIT_SSH_COMMAND"] = env_ssh
    try:
        res = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=live,
                             capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "timed out contacting the remote"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    if res.returncode == 0:
        return True, "remote reachable; push will authenticate"
    err = (res.stderr or res.stdout).strip()
    if "Permission denied" in err or "publickey" in err:
        err += ("\n\nThe key is not authorized for this repository. Add its PUBLIC half as a "
                "deploy key on the remote, with write access enabled.")
    return False, err[-800:]


def install_key(project: str, live: str, private_key: str) -> KeyStatus:
    """Write a pasted private key and point the live repo's git at it."""
    body = private_key.strip()
    if not _PRIVATE_KEY_RE.search(body):
        raise DeployKeyError(
            "that does not look like an SSH private key -- paste the PRIVATE half "
            "(the file WITHOUT the .pub extension), including the BEGIN/END lines")
    if not body.endswith("\n"):
        body += "\n"

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEYS_DIR, stat.S_IRWXU)  # 0700 -- ssh refuses world-readable key dirs
    kf = _key_path(project)
    # Write via a 0600 file from the start; never create it readable and chmod
    # after, which leaves a window where the key is world-readable on disk.
    fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)

    if public_key_of(kf) is None:
        kf.unlink(missing_ok=True)
        raise DeployKeyError(
            "the key could not be read by ssh-keygen -- it may be corrupted, or "
            "passphrase-protected (deploy keys must have no passphrase, since nothing "
            "can type one during an unattended push)")

    configure_repo(live, kf)
    return status(project, live)


def generate_key(project: str, live: str, comment: str | None = None) -> KeyStatus:
    """Create a fresh ed25519 keypair for this project.

    Better UX than pasting: the operator never handles the private half at
    all -- they copy the PUBLIC key out of the result and add it on the
    remote.
    """
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEYS_DIR, stat.S_IRWXU)
    kf = _key_path(project)
    if kf.exists():
        kf.unlink()
    (KEYS_DIR / f"{project}.key.pub").unlink(missing_ok=True)
    ok, out = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                    "-C", comment or f"3d-agent-{project}", "-f", str(kf)])
    if not ok:
        raise DeployKeyError(f"ssh-keygen failed: {out}")
    os.chmod(kf, 0o600)
    configure_repo(live, kf)
    return status(project, live)


def configure_repo(live: str, key_file: Path) -> tuple[bool, str]:
    """Point ONE repo's git transport at this key. Per-repo on purpose: a
    global setting would sign every other project's pushes with it too."""
    cmd = (f"ssh -i {key_file} -o IdentitiesOnly=yes "
           f"-o StrictHostKeyChecking=accept-new")
    return _git(live, ["config", "core.sshCommand", cmd])


def remove_key(project: str, live: str) -> KeyStatus:
    """Delete the key and unset the repo's pointer to it."""
    kf = _key_path(project)
    kf.unlink(missing_ok=True)
    (KEYS_DIR / f"{project}.key.pub").unlink(missing_ok=True)
    _git(live, ["config", "--unset", "core.sshCommand"])
    return status(project, live)
