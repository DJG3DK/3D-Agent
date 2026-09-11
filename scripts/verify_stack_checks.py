#!/usr/bin/env python3
"""Run the checks onboarding proposes, in a real toolchain, against a real repo.

Detection is unit-tested (tests/test_provisioning.py), but a unit test only
proves we produce the command we meant to produce. It cannot tell us that
`cargo fmt -- --check` is the spelling this cargo accepts, or that
`go vet ./...` is the one that works from a module root. Getting that wrong
is invisible until a project is onboarded and every review fails on a usage
error -- the exact shape of failure the review gate is supposed to catch, in
the gate itself.

So this script builds a throwaway repo per stack, asks provisioning what it
would run, and then RUNS it: on this host for Make and pytest, and in the
official toolchain image for Go, Rust and Ruby (read-only network off, so a
pass also proves the fixture needed nothing from the network).

    python3 scripts/verify_stack_checks.py            # everything
    python3 scripts/verify_stack_checks.py --no-docker  # host stacks only

Not part of CI: pulling three language images costs more than it is worth on
every push. Run it when the detection rules in agent/provisioning.py change.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMAGES = {
    "go": "golang:1.22-alpine",
    # Built locally from rust:1 -- see RUST_DOCKERFILE. The official image
    # installs rustup's minimal profile, which has neither rustfmt nor
    # clippy, so `cargo fmt` in it fails with "run rustup component add"
    # rather than with a formatting verdict. Adding the components to the
    # image, instead of before each check, keeps the checks themselves
    # running with --network none.
    "rust": "3d-agent-verify-rust:latest",
    "ruby": "ruby:3.3-alpine",
}
RUST_DOCKERFILE = "FROM rust:1\nRUN rustup component add rustfmt clippy\n"
CONTAINER_ENV = {
    "go": ["-e", "HOME=/tmp", "-e", "GOCACHE=/tmp/gocache", "-e", "GOPATH=/tmp/gopath",
           "-e", "GOFLAGS=-mod=mod"],
    "rust": ["-e", "HOME=/tmp", "-e", "CARGO_HOME=/tmp/cargo"],
    "ruby": ["-e", "HOME=/tmp"],
}


def fixture_go(root: Path) -> Path:
    repo = root / "go-svc"
    (repo / "internal").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/gosvc\n\ngo 1.22\n")
    (repo / "main.go").write_text('package main\n\nfunc main() { println(Sum(1, 2)) }\n\n'
                                  'func Sum(a, b int) int { return a + b }\n')
    (repo / "main_test.go").write_text(
        'package main\n\nimport "testing"\n\n'
        'func TestSum(t *testing.T) { if Sum(1, 2) != 3 { t.Fatal("bad") } }\n')
    return repo


def fixture_rust(root: Path) -> Path:
    repo = root / "rust-crate"
    (repo / "src").mkdir(parents=True)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "crate_under_test"\nversion = "0.1.0"\nedition = "2021"\n')
    (repo / "rustfmt.toml").write_text("edition = \"2021\"\n")
    (repo / "clippy.toml").write_text("msrv = \"1.70\"\n")
    (repo / "src" / "lib.rs").write_text(
        "pub fn sum(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    #[test]\n    fn it_adds() {\n        assert_eq!(sum(1, 2), 3);\n    }\n}\n")
    return repo


def fixture_ruby(root: Path) -> Path:
    # No Gemfile on purpose: rake and minitest are default gems, so the
    # detected command runs with nothing installed and no network.
    repo = root / "ruby-app"
    (repo / "test").mkdir(parents=True)
    (repo / ".ruby-version").write_text("3.3.0\n")
    (repo / "Rakefile").write_text(
        'require "rake/testtask"\n\n'
        'Rake::TestTask.new(:test) do |t|\n  t.pattern = "test/**/*_test.rb"\nend\n')
    (repo / "test" / "calc_test.rb").write_text(
        'require "minitest/autorun"\n\n'
        'class CalcTest < Minitest::Test\n  def test_adds\n    assert_equal 2, 1 + 1\n  end\nend\n')
    return repo


def fixture_make(root: Path) -> Path:
    repo = root / "make-proj"
    repo.mkdir(parents=True)
    (repo / "Makefile").write_text("test:\n\t@echo running suite\nlint:\n\t@echo linting\n")
    return repo


def fixture_python(root: Path) -> Path:
    repo = root / "py-proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'pyproj'\nversion = '0'\n")
    (repo / "tests" / "test_math.py").write_text("def test_adds():\n    assert 1 + 1 == 2\n")
    return repo


FIXTURES = {
    "go": fixture_go,
    "rust": fixture_rust,
    "ruby": fixture_ruby,
    "make": fixture_make,
    "python": fixture_python,
}


# A check that cannot fail is not a gate. After every proposed check has run
# clean, one assertion per stack is broken and the test check is re-run: it
# must come back non-zero. Without this the whole script would pass just as
# happily on a command that exits 0 no matter what the code does -- which is
# exactly what `cargo clippy` without `-D warnings` would have been.
def break_go(repo: Path) -> None:
    (repo / "main_test.go").write_text(
        'package main\n\nimport "testing"\n\n'
        'func TestSum(t *testing.T) { if Sum(1, 2) != 4 { t.Fatal("deliberate") } }\n')


def break_rust(repo: Path) -> None:
    text = (repo / "src" / "lib.rs").read_text()
    (repo / "src" / "lib.rs").write_text(text.replace("sum(1, 2), 3", "sum(1, 2), 4"))


def break_ruby(repo: Path) -> None:
    (repo / "test" / "calc_test.rb").write_text(
        'require "minitest/autorun"\n\n'
        'class CalcTest < Minitest::Test\n  def test_adds\n    assert_equal 3, 1 + 1\n  end\nend\n')


def break_make(repo: Path) -> None:
    (repo / "Makefile").write_text("test:\n\t@exit 1\nlint:\n\t@echo linting\n")


def break_python(repo: Path) -> None:
    (repo / "tests" / "test_math.py").write_text("def test_adds():\n    assert 1 + 1 == 3\n")


BREAKERS = {
    "go": break_go, "rust": break_rust, "ruby": break_ruby,
    "make": break_make, "python": break_python,
}
def git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)


def run_check(stack: str, repo: Path, check: dict, use_docker: bool) -> tuple[bool, str]:
    cmd = [check["cmd"], *check["args"]]
    if stack in IMAGES:
        if not use_docker:
            return None, "needs docker"
        argv = ["docker", "run", "--rm", "--network", "none",
                "-v", f"{repo}:/w", "-w", "/w", *CONTAINER_ENV[stack], IMAGES[stack], *cmd]
    else:
        argv = cmd
        if check["cmd"] == "python":
            argv = [sys.executable, *check["args"]]
    try:
        res = subprocess.run(argv, cwd=None if stack in IMAGES else repo,
                             capture_output=True, text=True, timeout=900)
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    tail = (res.stdout + res.stderr).strip().splitlines()
    return res.returncode == 0, (tail[-1][:120] if tail else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-docker", action="store_true")
    args = ap.parse_args()
    use_docker = not args.no_docker and bool(shutil.which("docker"))

    root = Path(tempfile.mkdtemp(prefix="stack-checks-"))
    os.environ["AGENT_PROJECT_ROOTS"] = str(root)
    os.environ["AGENT_SANDBOX_ROOT"] = str(root / "workspaces")
    from agent import provisioning as prov

    if use_docker:
        for stack, image in IMAGES.items():
            if stack == "rust":
                print(f"building {image} ...", flush=True)
                built = subprocess.run(["docker", "build", "-q", "-t", image, "-"],
                                       input=RUST_DOCKERFILE, text=True,
                                       capture_output=True, timeout=1800)
                if built.returncode != 0:
                    print(f"  could not build {image}: {built.stderr.strip()[:300]}")
                    return 2
                continue
            print(f"pulling {image} ...", flush=True)
            if subprocess.run(["docker", "pull", "-q", image]).returncode != 0:
                print(f"  could not pull {image}; rerun with --no-docker")
                return 2

    failures = 0
    try:
        for stack, build in FIXTURES.items():
            repo = build(root)
            git_init(repo)
            report = prov.detect_project(str(repo))
            print(f"\n{stack}: {[c['name'] for c in report.checks]}")
            if not report.checks:
                print("  NO CHECKS DETECTED")
                failures += 1
                continue
            for check in report.checks:
                shown = " ".join([check["cmd"], *check["args"]])
                ok, detail = run_check(stack, repo, check, use_docker)
                label = "skip" if ok is None else ("ok  " if ok else "FAIL")
                print(f"  {label} {check['name']:<6} {shown}" + (f"   -- {detail}" if detail else ""))
                if ok is False:
                    failures += 1

            test_check = next((c for c in report.checks if c["name"] == "test"), None)
            if test_check is not None:
                BREAKERS[stack](repo)
                ok, detail = run_check(stack, repo, test_check, use_docker)
                if ok is None:
                    print("  skip gates  (needs docker)")
                elif ok:
                    print("  FAIL gates  the suite passed on deliberately broken code")
                    failures += 1
                else:
                    print("  ok   gates  broken code fails the test check")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{'all proposed checks ran clean' if not failures else f'{failures} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
