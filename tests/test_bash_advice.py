"""bash is for running things; reading and editing have their own tools.

Live, 2026-09-12: the test-writer subagent made 229 bash calls against ONE
edit and ONE write. It patched JavaScript with `python3 - <<'PY' ...
open(p,'w').write(s.replace(...))` and read files with `cat` and `sed -n`,
while holding `read`, `write` and `edit` the whole time.

That is a wall-clock problem, not a style one: every bash call starts a
container, and the median gap between that subagent's model calls was 11
seconds. The in-process tools cost none of it.

A note on the result, never a refusal -- writing a scratch script to RUN is a
fair use of a shell, and no pattern can tell every case apart.
"""

from agent.tools.bash_advice import EDIT_NOTE, READ_NOTE, advice_for, kind


def test_the_heredoc_patch_that_started_this():
    cmd = ("cd /workspace && python3 - <<'PY'\n"
           "p='/workspace/tests/test_approach_gate_parity.js'\n"
           "s=open(p).read()\n"
           "open(p,'w').write(s.replace('a','b'))\n"
           "PY")
    assert advice_for(cmd) == EDIT_NOTE
    assert kind(cmd) == "write"


def test_every_common_way_to_write_a_file_through_a_shell():
    for cmd in (
        "cd /workspace && cat > tests/new_test.js <<'EOF'\nconsole.log(1)\nEOF",
        "cat >> src/app.js <<'EOF'\nmore\nEOF",
        "cd /workspace && sed -i 's/foo/bar/' src/core/bot.js",
        "sed -i.bak -e 's#a#b#' file.js",
        "perl -pi -e 's/x/y/' src/thing.js",
        "echo hi | tee src/notes.txt",
        "tee -a src/notes.txt < /tmp/x",
        "node -e \"require('fs').writeFileSync('src/a.js', 'x')\"",
        "python3 -c \"from pathlib import Path; Path('a.js').write_text('x')\"",
    ):
        assert advice_for(cmd) == EDIT_NOTE, cmd


def test_reading_one_file_is_pointed_at_the_read_tool():
    for cmd in (
        "cd /workspace && cat package.json",
        "cat src/core/backtester.js",
        "cd /workspace && sed -n '1,80p' src/strategies/srDivergence.js",
        "head src/app.js",
        "cd /workspace && tail tests/test_x.js",
    ):
        assert advice_for(cmd) == READ_NOTE, cmd
    assert kind("cat package.json") == "read"


def test_running_things_is_left_alone():
    """The whole point of the tool. A false nudge here trains the model to
    ignore the real ones."""
    for cmd in (
        "cd /workspace && npm test",
        "cd /workspace && node tests/test_trigger_repaint.js 2>&1 | tail -40",
        "cd /workspace && rg -n 'someSymbol' src frontend/src",
        "cd /workspace && git status --short",
        "cd /workspace && bash scripts/mutateTriggerRepaint.sh",
        "cd /workspace && ls -la src",
        "cat package.json | jq .scripts",          # a read piped into real work
        "cd /workspace && grep -c foo src/*.js",
        "python3 -c \"print(1+1)\"",
        "cd /workspace && node -e \"console.log(require('./package.json').name)\"",
    ):
        assert advice_for(cmd) is None, cmd


def test_a_write_wins_over_a_read_when_a_command_does_both():
    cmd = "cd /workspace && cat src/a.js && sed -i 's/x/y/' src/a.js"
    assert advice_for(cmd) == EDIT_NOTE


def test_empty_and_nonsense_commands_do_not_raise():
    for cmd in ("", "   ", "\n", "&&", "|||"):
        assert advice_for(cmd) is None
        assert kind(cmd) is None


def test_the_notes_say_why_not_just_what():
    """A rule with no reason gets argued with. Both notes name the container
    cost, which is the thing the model cannot see from inside."""
    for note in (EDIT_NOTE, READ_NOTE):
        assert "container" in note
        assert "RUNNING things" in note
