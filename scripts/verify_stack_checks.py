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
    "elixir": "elixir:1.16-alpine",
    "java": "maven:3.9-eclipse-temurin-21",
    "php": "composer:2",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
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
    "elixir": ["-e", "HOME=/tmp", "-e", "MIX_ENV=test"],
    "java": ["-e", "HOME=/tmp", "-e", "MAVEN_OPTS=-Dmaven.repo.local=/tmp/m2"],
    "php": ["-e", "HOME=/tmp", "-e", "COMPOSER_HOME=/tmp/composer"],
    "dotnet": ["-e", "HOME=/tmp", "-e", "DOTNET_CLI_TELEMETRY_OPTOUT=1",
               "-e", "DOTNET_NOLOGO=1", "-e", "NUGET_PACKAGES=/tmp/nuget"],
}

# Stacks whose fixture cannot build without fetching. Maven downloads its own
# plugins before it can run a single test, NuGet the test framework, Composer
# the phpunit package -- none of that is optional and none of it is under this
# script's control. The command spelling is still what is being verified; the
# --network none default just stops being available as extra evidence.
NEEDS_NETWORK = {"java", "php", "dotnet"}

# Some toolchains need a step before the checks that is not itself a check:
# fetching dependencies the fixture declares. Run once, before the proposed
# commands, so a failure there is not reported as the check failing.
SETUP = {
    "php": ["composer", "install", "--no-interaction", "--no-progress"],
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


def fixture_elixir(root: Path) -> Path:
    repo = root / "elixir-app"
    (repo / "lib").mkdir(parents=True)
    (repo / "test").mkdir()
    (repo / "mix.exs").write_text(
        'defmodule Calc.MixProject do\n'
        '  use Mix.Project\n'
        '  def project, do: [app: :calc, version: "0.1.0", elixir: "~> 1.14"]\n'
        '  def application, do: []\n'
        'end\n')
    (repo / ".formatter.exs").write_text('[inputs: ["lib/**/*.ex", "test/**/*.exs", "mix.exs"]]\n')
    (repo / "lib" / "calc.ex").write_text(
        "defmodule Calc do\n  def sum(a, b), do: a + b\nend\n")
    (repo / "test" / "test_helper.exs").write_text("ExUnit.start()\n")
    (repo / "test" / "calc_test.exs").write_text(
        "defmodule CalcTest do\n  use ExUnit.Case\n\n"
        "  test \"adds\" do\n    assert Calc.sum(1, 2) == 3\n  end\nend\n")
    return repo


def fixture_java(root: Path) -> Path:
    repo = root / "java-svc"
    (repo / "src" / "main" / "java").mkdir(parents=True)
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        '  <modelVersion>4.0.0</modelVersion>\n'
        '  <groupId>example</groupId>\n  <artifactId>svc</artifactId>\n'
        '  <version>1.0</version>\n'
        '  <properties>\n'
        '    <maven.compiler.release>17</maven.compiler.release>\n'
        '    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n'
        '  </properties>\n'
        '  <dependencies>\n'
        '    <dependency>\n'
        '      <groupId>org.junit.jupiter</groupId>\n'
        '      <artifactId>junit-jupiter</artifactId>\n'
        '      <version>5.10.2</version>\n'
        '      <scope>test</scope>\n'
        '    </dependency>\n'
        '  </dependencies>\n'
        '  <build><plugins><plugin>\n'
        '    <groupId>org.apache.maven.plugins</groupId>\n'
        '    <artifactId>maven-surefire-plugin</artifactId>\n'
        '    <version>3.2.5</version>\n'
        '  </plugin></plugins></build>\n'
        '</project>\n')
    (repo / "src" / "main" / "java" / "Calc.java").write_text(
        "public class Calc {\n  public static int sum(int a, int b) { return a + b; }\n}\n")
    (repo / "src" / "test" / "java" / "CalcTest.java").write_text(
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class CalcTest {\n  @Test void adds() { assertEquals(3, Calc.sum(1, 2)); }\n}\n")
    return repo


def fixture_php(root: Path) -> Path:
    repo = root / "php-app"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "composer.json").write_text(
        '{\n  "name": "example/app",\n'
        '  "require-dev": {"phpunit/phpunit": "^10"},\n'
        '  "autoload": {"psr-4": {"App\\\\": "src/"}},\n'
        '  "scripts": {"test": "phpunit --colors=never"},\n'
        '  "config": {"vendor-dir": "vendor"}\n}\n')
    (repo / "phpunit.xml").write_text(
        '<phpunit bootstrap="vendor/autoload.php">\n'
        '  <testsuites><testsuite name="unit"><directory>tests</directory></testsuite></testsuites>\n'
        '</phpunit>\n')
    (repo / "src" / "Calc.php").write_text(
        "<?php\nnamespace App;\nclass Calc { public static function sum($a, $b) { return $a + $b; } }\n")
    (repo / "tests" / "CalcTest.php").write_text(
        "<?php\nuse PHPUnit\\Framework\\TestCase;\nuse App\\Calc;\n\n"
        "class CalcTest extends TestCase {\n"
        "  public function testAdds(): void { $this->assertSame(3, Calc::sum(1, 2)); }\n}\n")
    return repo


def fixture_dotnet(root: Path) -> Path:
    repo = root / "dotnet-app"
    repo.mkdir(parents=True)
    (repo / "App.Tests.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net8.0</TargetFramework>\n'
        '    <IsPackable>false</IsPackable>\n'
        '    <Nullable>enable</Nullable>\n'
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.9.0" />\n'
        '    <PackageReference Include="xunit" Version="2.7.0" />\n'
        '    <PackageReference Include="xunit.runner.visualstudio" Version="2.5.7" />\n'
        '  </ItemGroup>\n'
        '</Project>\n')
    (repo / "Calc.cs").write_text(
        "namespace App;\n\npublic static class Calc\n{\n"
        "    public static int Sum(int a, int b) => a + b;\n}\n")
    (repo / "CalcTests.cs").write_text(
        "using Xunit;\nusing App;\n\npublic class CalcTests\n{\n"
        "    [Fact]\n    public void Adds() => Assert.Equal(3, Calc.Sum(1, 2));\n}\n")
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
    "elixir": fixture_elixir,
    "java": fixture_java,
    "php": fixture_php,
    "dotnet": fixture_dotnet,
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
    # Deliberately a different LENGTH, and the caches go too. CPython
    # invalidates a .pyc on (mtime, size), and pytest's assertion rewriter
    # caches under the same rule -- an edit of equal size inside the same
    # second reran the stale bytecode and the "broken" suite passed, which
    # made this very check flap.
    (repo / "tests" / "test_math.py").write_text(
        "def test_adds():\n    assert 1 + 1 == 3, 'deliberately broken'\n")
    for cache in (*repo.rglob("__pycache__"), *repo.rglob(".pytest_cache")):
        shutil.rmtree(cache, ignore_errors=True)


def break_elixir(repo: Path) -> None:
    (repo / "test" / "calc_test.exs").write_text(
        "defmodule CalcTest do\n  use ExUnit.Case\n\n"
        "  test \"adds\" do\n    assert Calc.sum(1, 2) == 4\n  end\nend\n")


def break_java(repo: Path) -> None:
    (repo / "src" / "test" / "java" / "CalcTest.java").write_text(
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class CalcTest {\n  @Test void adds() { assertEquals(4, Calc.sum(1, 2)); }\n}\n")


def break_php(repo: Path) -> None:
    (repo / "tests" / "CalcTest.php").write_text(
        "<?php\nuse PHPUnit\\Framework\\TestCase;\nuse App\\Calc;\n\n"
        "class CalcTest extends TestCase {\n"
        "  public function testAdds(): void { $this->assertSame(4, Calc::sum(1, 2)); }\n}\n")


def break_dotnet(repo: Path) -> None:
    (repo / "CalcTests.cs").write_text(
        "using Xunit;\nusing App;\n\npublic class CalcTests\n{\n"
        "    [Fact]\n    public void Adds() => Assert.Equal(4, Calc.Sum(1, 2));\n}\n")


BREAKERS = {
    "go": break_go, "rust": break_rust, "ruby": break_ruby,
    "elixir": break_elixir, "java": break_java, "php": break_php,
    "dotnet": break_dotnet, "make": break_make, "python": break_python,
}
def git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)


def run_check(stack: str, repo: Path, check: dict, use_docker: bool) -> tuple[bool, str]:
    cmd = [check["cmd"], *check["args"]]
    if stack in IMAGES:
        if not use_docker:
            return None, "needs docker"
        net = "bridge" if stack in NEEDS_NETWORK else "none"
        argv = ["docker", "run", "--rm", "--network", net,
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
            print(f"\n{stack}: {[c['name'] for c in report.checks]}", flush=True)
            if stack in SETUP and use_docker:
                cmd = SETUP[stack]
                ok, detail = run_check(stack, repo, {"cmd": cmd[0], "args": cmd[1:]}, use_docker)
                print(f"  {'ok  ' if ok else 'FAIL'} setup  {' '.join(cmd)}"
                      + (f"   -- {detail}" if detail else ""), flush=True)
                if not ok:
                    failures += 1
                    continue
            if not report.checks:
                print("  NO CHECKS DETECTED")
                failures += 1
                continue
            for check in report.checks:
                shown = " ".join([check["cmd"], *check["args"]])
                ok, detail = run_check(stack, repo, check, use_docker)
                label = "skip" if ok is None else ("ok  " if ok else "FAIL")
                print(f"  {label} {check['name']:<8} {shown}"
                      + (f"   -- {detail}" if detail else ""), flush=True)
                if ok is False:
                    failures += 1

            # A check that cannot fail is not a gate, so every stack's test
            # check is re-run against a deliberate break.
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
