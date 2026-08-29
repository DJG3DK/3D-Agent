"""Project onboarding: inspect a directory, propose a configuration, and
provision it as a project this agent can work in.

Why detection PROPOSES rather than decides
------------------------------------------
Everything in a project's config falls into one of three buckets:

1. Derivable with certainty (is it a git repo, which package manager,
   which npm scripts exist). Detected and applied.
2. Derivable as a candidate (which gitignored files look like secrets the
   test suite needs, which pm2 apps serve this path). Detected, PROPOSED,
   and shown to the operator to accept or reject.
3. Not derivable at all. The canonical example lives in this deployment:
   a trading-bot project's `test:auth` and `test:routes` make real HTTP calls to
   a live service -- the live trading bot -- and `test:routes` exercises
   POST /trade/open. Running them unattended would place real orders for
   zero review signal. No static analysis reliably distinguishes "hits a
   test server" from "hits your production system", so this module FLAGS
   the suspicion and refuses to silently enable such a script.

So the wizard is deliberately detect -> confirm -> provision, never a
one-click guess. A wrong guess here doesn't produce a bad config; it
produces an unattended agent running destructive commands against
production.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Files that look like credentials, are gitignored, and therefore never
# reach a worktree checkout -- the review service copies them from the live
# checkout so the suite runs the way production would.
_SECRET_NAME_HINTS = re.compile(
    r"(^|/)(\.env($|\.)|.*\.env$|secrets?\.|credentials?\.|auth\.json|keys?\.json|"
    r"token|\.pem$|\.key$|serviceaccount)",
    re.IGNORECASE,
)

# Network calls inside a test file. A match doesn't prove the test hits
# production -- it proves we cannot prove it doesn't.
_NETWORK_CALL = re.compile(
    r"(fetch\s*\(|axios|http\.request|https\.request|got\s*\(|superagent|"
    r"requests\.(get|post|put|delete)|urllib|httpx\.|127\.0\.0\.1|localhost:\d+)",
    re.IGNORECASE,
)

_SCRIPT_REF = re.compile(r"[\w./-]+\.(?:js|mjs|cjs|ts|tsx|py)")

# Directories never worth mounting or scanning.
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", "vendor",
    ".cache", "target", ".gradle",
}

CHECK_TIMEOUT_MS_DEFAULT = 300_000


class ProvisioningError(Exception):
    """Raised for an input the operator must fix (bad path, name clash)."""


class PathNotAllowedError(ProvisioningError):
    """The requested path is outside every configured project root."""


# Where projects may live. Onboarding hands an agent bash and write access to
# whatever it points at, and makes the review service COPY the files listed as
# secrets into a worktree -- so "any absolute path on the host" is far too much
# authority to grant from an HTTP request, even an admin's. Containment is the
# boundary; the admin check is only who may ask.
#
# Colon-separated, like PATH. Defaults to /home, which covers the normal
# layout without reaching /root, /etc, or the agent's own credentials.
def allowed_roots() -> list[str]:
    raw = os.environ.get("AGENT_PROJECT_ROOTS", "/home")
    return [os.path.realpath(r) for r in raw.split(":") if r.strip()]


def sandbox_root() -> str:
    """Server-owned, never client-supplied: the worktree location is a write
    primitive, so it is derived from config plus the project name."""
    return os.path.realpath(os.environ.get("AGENT_SANDBOX_ROOT", "/home/agent-workspaces"))


def _agent_own_roots() -> list[str]:
    """Every path that IS this agent's own code.

    Plural because this file may be running from a git worktree of the agent
    repo (that is how its own features get developed). Checking only the
    module's parent would then guard the worktree while leaving the real
    repo onboardable -- so follow the worktree's .git pointer file back to
    the main checkout and block both.
    """
    own = os.path.realpath(Path(__file__).resolve().parent.parent)
    roots = [own]
    dotgit = os.path.join(own, ".git")
    if os.path.isfile(dotgit):
        try:
            gitdir = open(dotgit).read().strip().removeprefix("gitdir:").strip()
            marker = f"{os.sep}worktrees{os.sep}"
            if marker in gitdir:
                # <main>/.git/worktrees/<name> -> <main>/.git -> <main>
                git_dir = gitdir[: gitdir.index(marker)]
                roots.append(os.path.realpath(os.path.dirname(git_dir)
                                              if os.path.basename(git_dir) == ".git" else git_dir))
        except OSError:
            pass
    return roots


def _is_within(child: str, parent: str) -> bool:
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    if child == parent:
        return True
    # rstrip so a parent of "/" compares against "/" rather than "//", which
    # would make every path read as outside it.
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def assert_path_allowed(path: str) -> str:
    """Resolve `path` and confirm it sits inside an allowed root.

    realpath first, so a symlink pointing out of an allowed root is judged by
    where it actually lands rather than by its own name.
    """
    if not os.path.isabs(path):
        raise ProvisioningError("path must be absolute")
    real = os.path.realpath(path)
    roots = allowed_roots()
    if not any(_is_within(real, root) for root in roots):
        raise PathNotAllowedError(
            f"{real} is outside the configured project roots ({', '.join(roots)}). "
            "Set AGENT_PROJECT_ROOTS to allow it.")
    if any(_is_within(real, own) for own in _agent_own_roots()):
        raise PathNotAllowedError(
            "refusing to onboard the agent's own repository -- a task merging into it "
            "would rewrite and restart the process running that task")
    if _is_within(real, sandbox_root()):
        raise PathNotAllowedError(
            f"{real} is inside the workspace root ({sandbox_root()}); onboard the LIVE "
            "repo, not an agent worktree")
    return real


def safe_relative(rel: str, base: str, *, must_exist: bool = True) -> str:
    """A repo-relative path that cannot escape the repo.

    These strings become filenames the review service copies OUT of the live
    checkout, so `../../root/.ssh/id_rsa` must never survive this function.
    """
    if os.path.isabs(rel):
        raise ProvisioningError(f"{rel!r} must be relative to the project root")
    joined = os.path.realpath(os.path.join(base, rel))
    if not _is_within(joined, base):
        raise ProvisioningError(f"{rel!r} escapes the project directory")
    if must_exist and not os.path.exists(joined):
        raise ProvisioningError(f"{rel!r} does not exist in the project")
    return rel.strip("/")


@dataclass
class Candidate:
    """A proposed config item the operator accepts or rejects.

    `enabled` is our RECOMMENDATION, not a decision -- the wizard sends back
    what the operator actually chose. Anything with a `warning` defaults to
    disabled: the safe default for something we cannot verify is off.
    """

    value: str
    reason: str
    enabled: bool = True
    warning: str | None = None


@dataclass
class DetectionReport:
    name: str
    live: str
    sandbox: str
    is_git_repo: bool = False
    package_manager: str | None = None       # npm | pnpm | yarn
    languages: list[str] = field(default_factory=list)
    node_modules_dirs: list[str] = field(default_factory=lambda: ["."])
    checks: list[dict] = field(default_factory=list)
    build_steps: list[dict] = field(default_factory=list)
    pm2_apps: list[Candidate] = field(default_factory=list)
    secret_files: list[Candidate] = field(default_factory=list)
    read_only_mounts: list[Candidate] = field(default_factory=list)
    risky_scripts: list[Candidate] = field(default_factory=list)
    db_env_file: str | None = None
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _gitignored_entries(live: Path) -> list[str]:
    """Literal (non-glob, non-negated) .gitignore entries, from the root file
    only. Globs are skipped deliberately: expanding them invites walking a
    huge tree, and a literal path is exactly the shape that names a real
    secret file or fixture directory."""
    out: list[str] = []
    gi = live / ".gitignore"
    if not gi.is_file():
        return out
    try:
        lines = gi.read_text().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if any(ch in line for ch in "*?[]"):
            continue
        out.append(line.strip("/"))
    return out


def _detect_package_manager(live: Path) -> str | None:
    if (live / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (live / "yarn.lock").is_file():
        return "yarn"
    if (live / "package.json").is_file():
        return "npm"
    return None


def _detect_languages(live: Path) -> list[str]:
    langs = []
    if (live / "package.json").is_file():
        langs.append("node")
    if any((live / f).is_file() for f in ("pyproject.toml", "requirements.txt", "pytest.ini", "setup.py")):
        langs.append("python")
    if (live / "go.mod").is_file():
        langs.append("go")
    if (live / "Cargo.toml").is_file():
        langs.append("rust")
    return langs


def _workspace_dirs(live: Path, pkg: dict) -> list[str]:
    """Directories that get their own node_modules in a monorepo. Derived
    from package.json `workspaces` / pnpm-workspace.yaml, expanded only one
    level (`apps/*` -> the real dirs), never by walking the whole tree."""
    dirs = ["."]
    patterns: list[str] = []
    ws = pkg.get("workspaces")
    if isinstance(ws, dict):
        patterns = list(ws.get("packages") or [])
    elif isinstance(ws, list):
        patterns = list(ws)
    pnpm_ws = live / "pnpm-workspace.yaml"
    if pnpm_ws.is_file():
        try:
            for line in pnpm_ws.read_text().splitlines():
                m = re.match(r"\s*-\s*['\"]?([^'\"]+)['\"]?\s*$", line)
                if m:
                    patterns.append(m.group(1))
        except OSError:
            pass
    for pat in patterns:
        pat = pat.strip()
        if pat.endswith("/*"):
            base = live / pat[:-2]
            if base.is_dir():
                for child in sorted(base.iterdir()):
                    if child.is_dir() and (child / "package.json").is_file():
                        dirs.append(str(child.relative_to(live)))
        elif pat and not any(c in pat for c in "*?"):
            if (live / pat / "package.json").is_file():
                dirs.append(pat)
    # dedupe, keep order
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _script_is_risky(live: Path, script_body: str) -> str | None:
    """Return a warning when a script's own body, or a file it references,
    makes network calls. See this module's docstring for why this exists."""
    if _NETWORK_CALL.search(script_body):
        return "the script command itself references a network address"
    for ref in _SCRIPT_REF.findall(script_body):
        target = live / ref
        if not target.is_file():
            continue
        try:
            body = target.read_text(errors="ignore")[:200_000]
        except OSError:
            continue
        if _NETWORK_CALL.search(body):
            return f"{ref} makes network calls -- confirm it does not target a live service"
    return None


def _detect_node_checks(live: Path, pkg: dict, pm: str) -> tuple[list[dict], list[Candidate]]:
    """Checks come from the repo's OWN scripts, never a reconstructed list --
    same principle agent/tools/checks.py already follows."""
    scripts: dict = pkg.get("scripts") or {}
    checks: list[dict] = []
    risky: list[Candidate] = []

    # A repo that declares test:review has stated which suites are safe in a
    # detached checkout. Prefer it and don't second-guess the rest.
    has_review = "test:review" in scripts
    for name in ("typecheck", "lint", "build"):
        if name in scripts:
            checks.append({"name": name, "dir": ".", "cmd": pm, "args": ["run", name],
                           "timeoutMs": CHECK_TIMEOUT_MS_DEFAULT})
    if has_review:
        checks.append({"name": "test", "dir": ".", "cmd": pm, "args": ["run", "test:review"],
                       "timeoutMs": 900_000})
    elif "test" in scripts:
        checks.append({"name": "test", "dir": ".", "cmd": pm, "args": ["run", "test"],
                       "timeoutMs": 900_000})

    for sname, body in scripts.items():
        if not sname.startswith("test") or not isinstance(body, str):
            continue
        why = _script_is_risky(live, body)
        if why:
            risky.append(Candidate(
                value=sname, reason=why, enabled=False,
                warning=("Excluded from automated review. A test that calls a live service can "
                         "act on production (this deployment learned that from a suite that "
                         "POSTed real trade orders). Enable only after reading it."),
            ))
    if risky and not has_review:
        # The repo has no curated safe-suite list AND has suspicious scripts.
        checks = [c for c in checks if c["name"] != "test"]
    return checks, risky


def _detect_python_checks(live: Path) -> list[dict]:
    checks: list[dict] = []
    if (live / "pytest.ini").is_file() or (live / "pyproject.toml").is_file() \
            or (live / "tests").is_dir():
        checks.append({"name": "test", "dir": ".", "cmd": "python", "args": ["-m", "pytest", "-q"],
                       "timeoutMs": 900_000})
    if (live / ".ruff.toml").is_file() or (live / "ruff.toml").is_file():
        checks.append({"name": "lint", "dir": ".", "cmd": "python", "args": ["-m", "ruff", "check", "."],
                       "timeoutMs": CHECK_TIMEOUT_MS_DEFAULT})
    return checks


def _detect_pm2_apps(live: Path) -> list[Candidate]:
    """pm2 apps whose working directory is inside this project. Proposed, not
    assumed: restarting the wrong app on merge takes down an unrelated
    service."""
    out: list[Candidate] = []
    pm2 = shutil.which("pm2")
    if not pm2:
        return out
    try:
        res = subprocess.run([pm2, "jlist"], capture_output=True, text=True, timeout=20)
        apps = json.loads(res.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return out
    real_live = os.path.realpath(live)
    for app in apps if isinstance(apps, list) else []:
        env = app.get("pm2_env") or {}
        for key in ("pm_cwd", "pm_exec_path", "cwd"):
            p = env.get(key) or app.get(key)
            if not p:
                continue
            if os.path.realpath(str(p)).startswith(real_live + os.sep) or os.path.realpath(str(p)) == real_live:
                out.append(Candidate(value=app.get("name", "?"),
                                     reason=f"pm2 app running from {p}"))
                break
    return out


def _detect_secrets_and_mounts(live: Path) -> tuple[list[Candidate], list[Candidate], str | None]:
    secrets: list[Candidate] = []
    mounts: list[Candidate] = []
    db_env: str | None = None
    for entry in _gitignored_entries(live):
        target = live / entry
        if not target.exists():
            continue
        if target.is_file():
            if _SECRET_NAME_HINTS.search(entry):
                secrets.append(Candidate(
                    value=entry,
                    reason="gitignored file that looks like credentials; the review checkout needs it",
                ))
                if db_env is None:
                    try:
                        if "DATABASE_URL" in target.read_text(errors="ignore")[:20_000]:
                            db_env = entry
                    except OSError:
                        pass
        elif target.is_dir() and target.name not in _SKIP_DIRS:
            try:
                has_content = any(True for _ in target.iterdir())
            except OSError:
                has_content = False
            if has_content:
                mounts.append(Candidate(
                    value=entry, enabled=False,
                    reason="gitignored directory with content -- mount read-only if tests need these fixtures",
                ))
    return secrets, mounts, db_env


def detect_project(live_path: str, sandbox_root: str | None = None,
                   existing_names: list[str] | None = None) -> DetectionReport:
    """Inspect a directory and propose a project configuration. Read-only --
    nothing is created or modified here."""
    live = Path(assert_path_allowed(live_path))
    name = live.name

    report = DetectionReport(name=name, live=str(live),
                             sandbox=str(Path(sandbox_root or globals()["sandbox_root"]()) / name))

    if not live.is_dir():
        report.blockers.append(f"{live} is not a directory")
        return report
    if not os.access(live, os.R_OK):
        report.blockers.append(f"{live} is not readable by the agent")
        return report
    if (live / ".git").exists():
        report.is_git_repo = True
    else:
        report.blockers.append(
            f"{live} is not a git repository -- the agent works on a per-task branch in a "
            "worktree of the live repo, so git is required")
    for existing in existing_names or []:
        if existing == name:
            report.blockers.append(f"a project named {name!r} is already configured")

    report.languages = _detect_languages(live)
    if not report.languages:
        report.warnings.append(
            "no recognized project manifest (package.json, pyproject.toml, go.mod, Cargo.toml) -- "
            "checks cannot be auto-detected; add them by hand after onboarding")

    pkg = _read_json(live / "package.json")
    pm = _detect_package_manager(live)
    report.package_manager = pm

    if pm:
        report.node_modules_dirs = _workspace_dirs(live, pkg)
        checks, risky = _detect_node_checks(live, pkg, pm)
        report.checks = checks
        report.risky_scripts = risky
        install = {"pnpm": ["install", "--frozen-lockfile"],
                   "yarn": ["install", "--frozen-lockfile"],
                   "npm": ["install", "--no-audit", "--no-fund"]}[pm]
        report.build_steps = [{"dir": ".", "cmd": pm, "args": install}]
        if "build" in (pkg.get("scripts") or {}):
            report.build_steps.append({"dir": ".", "cmd": pm, "args": ["run", "build"]})
        for d in report.node_modules_dirs[1:]:
            sub = _read_json(live / d / "package.json")
            if "build" in (sub.get("scripts") or {}):
                report.build_steps.append({"dir": d, "cmd": pm, "args": ["run", "build"]})
    elif "python" in report.languages:
        report.checks = _detect_python_checks(live)

    if not report.checks:
        report.warnings.append(
            "no automated checks detected -- the review gate will have nothing to run, so "
            "every change ships on human review alone")

    report.pm2_apps = _detect_pm2_apps(live)
    if not report.pm2_apps:
        report.warnings.append(
            "no pm2 app found serving this path -- merges will build but not restart anything")

    secrets, mounts, db_env = _detect_secrets_and_mounts(live)
    report.secret_files = secrets
    report.read_only_mounts = mounts
    report.db_env_file = db_env
    if report.risky_scripts:
        report.warnings.append(
            f"{len(report.risky_scripts)} test script(s) make network calls and were left "
            "disabled -- review each one before enabling")
    return report


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str, timeout: int = 120) -> tuple[bool, str]:
    try:
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def validate_choices(report: DetectionReport, choices: dict) -> dict:
    """Confirm the operator's answers are a SUBSET of what detection offered.

    The wizard is an approval step, not an authoring step. Without this, the
    `checks` and `build` arrays -- which the review and deploy services
    execute verbatim -- would be arbitrary commands supplied over HTTP, and
    `secretFiles` would be arbitrary paths the reviewer copies out of the
    live checkout. So every submitted item must match something this server
    itself proposed, and paths are re-checked for containment rather than
    trusted because they appeared in a report.

    Narrowing is always allowed; adding never is.
    """
    live = report.live
    offered_checks = {c["name"]: c for c in report.checks}
    # A flagged script becomes selectable only under its own detected name.
    for r in report.risky_scripts:
        offered_checks.setdefault(r.value, {
            "name": r.value, "dir": ".",
            "cmd": report.package_manager or "npm", "args": ["run", r.value],
            "timeoutMs": 900_000,
        })
    offered_builds = {json.dumps(b, sort_keys=True) for b in report.build_steps}
    offered_secrets = {c.value for c in report.secret_files}
    offered_mounts = {c.value for c in report.read_only_mounts}
    offered_apps = {c.value for c in report.pm2_apps}
    offered_nm = set(report.node_modules_dirs)

    clean: dict = {}

    checks = []
    for c in choices.get("checks") or []:
        name = (c or {}).get("name")
        if name not in offered_checks:
            raise ProvisioningError(
                f"check {name!r} was not proposed for this project -- the wizard can only "
                "confirm detected commands, not introduce new ones")
        checks.append(offered_checks[name])   # OUR version, never the client's cmd/args
    clean["checks"] = checks

    builds = []
    for b in choices.get("build_steps") or []:
        if json.dumps(b, sort_keys=True) not in offered_builds:
            raise ProvisioningError("build step was not proposed for this project")
        builds.append(b)
    clean["build_steps"] = builds

    def _subset(key: str, offered: set, *, relative_to: str | None = None) -> list[str]:
        out = []
        for v in choices.get(key) or []:
            if v not in offered:
                raise ProvisioningError(f"{v!r} was not proposed as {key.replace('_', ' ')}")
            out.append(safe_relative(v, relative_to) if relative_to else v)
        return out

    clean["secret_files"] = _subset("secret_files", offered_secrets, relative_to=live)
    clean["read_only_mounts"] = _subset("read_only_mounts", offered_mounts, relative_to=live)
    clean["pm2_apps"] = _subset("pm2_apps", offered_apps)
    clean["node_modules_dirs"] = _subset("node_modules_dirs", offered_nm, relative_to=live)

    db = choices.get("db_env_file")
    if db:
        if db != report.db_env_file:
            raise ProvisioningError("db_env_file was not the detected one")
        clean["db_env_file"] = safe_relative(db, live)
    return clean


def config_from_choices(report_name: str, live: str, sandbox: str, choices: dict) -> dict:
    """Build the projects.json entry from what the OPERATOR confirmed.

    `choices` is the wizard's payload, not the detection report -- the two
    are deliberately different objects so an operator's rejection can never
    be silently overwritten by a re-detection.
    """
    entry: dict = {"live": live, "sandbox": sandbox}
    if choices.get("db_env_file"):
        entry["db_env_file"] = choices["db_env_file"]

    review: dict = {}
    if choices.get("secret_files"):
        review["secretFiles"] = list(choices["secret_files"])
    if choices.get("read_only_mounts"):
        review["readOnlyMounts"] = list(choices["read_only_mounts"])
    if choices.get("node_modules_dirs"):
        review["nodeModulesDirs"] = list(choices["node_modules_dirs"])
    if choices.get("checks"):
        review["checks"] = list(choices["checks"])
    if review:
        entry["review"] = review

    deploy: dict = {}
    if choices.get("pm2_apps"):
        deploy["pm2Apps"] = list(choices["pm2_apps"])
    if choices.get("build_steps"):
        deploy["build"] = list(choices["build_steps"])
    if deploy:
        entry["deploy"] = deploy
    return entry


def write_project_entry(projects_path: Path, name: str, entry: dict) -> None:
    """Atomic add of one project to projects.json. Atomic because this file
    is read at import by the API, the reviewer, and the deploy service -- a
    half-written file breaks all three at once."""
    if projects_path.exists():
        data = json.loads(projects_path.read_text())
    else:
        data = {"projects": {}}
    data.setdefault("projects", {})
    if name in data["projects"]:
        raise ProvisioningError(f"{name!r} is already in projects.json")
    data["projects"][name] = entry
    tmp = projects_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, projects_path)


def create_worktree(live: str, sandbox: str, branch: str = "agent-base") -> tuple[bool, str]:
    """Create the agent's workspace as a git worktree of the live repo.

    A worktree, not a clone: tasks commit to a per-task branch that is a
    plain local ref in the live repo, which is what lets the review service
    read the branch directly with no remote in between.
    """
    if os.path.exists(sandbox):
        if os.path.isdir(os.path.join(sandbox, ".git")) or os.path.isfile(os.path.join(sandbox, ".git")):
            return True, f"worktree already exists at {sandbox}"
        return False, f"{sandbox} exists and is not a git worktree -- refusing to overwrite"
    os.makedirs(os.path.dirname(sandbox), exist_ok=True)
    ok, out = _run_git(["worktree", "add", sandbox, "-b", branch], cwd=live)
    if not ok and "already exists" in out:
        # Branch left behind by a previous onboarding of the same repo.
        ok, out = _run_git(["worktree", "add", sandbox, branch], cwd=live)
    return ok, out
