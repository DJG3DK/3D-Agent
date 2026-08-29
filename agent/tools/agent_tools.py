"""LangChain-tool wrappers around the hardened file/shell primitives, scoped to
a single repo root.

Deliberately not using deepagents' own default FilesystemBackend-driven file
tools or local-execution backend -- these wrappers already carry the
path-escape guard and the shell hardening described in shell.py's own
docstring, and the deepagents docs themselves warn against FilesystemBackend
in a long-running server context.

Large output handling: deepagents' own automatic tool-result offloading
(large output -> written to disk, replaced with a pointer + preview, full
content still reachable via read_file) only wraps deepagents' own filesystem
tools, not these custom ones. The same offload-and-preview pattern is
reimplemented here (reusing deepagents' own `_create_content_preview`
formatter for a consistent look) rather than a flat truncation cap, since a
long tool-call loop re-sends every prior round's full result on every
subsequent call -- keeping output small in the active conversation still
matters, but offloading does that without permanently discarding anything
past the cutoff. Offloaded content lands in the composite backend's default
(ephemeral, thread-scoped) StateBackend route -- correctly scoped to the
current task, not persisted cross-task, matching deepagents' own default
offload behavior.
"""

import json
import mimetypes
import os
import uuid

from langchain_core.tools import tool

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware._message_eviction import _create_content_preview

from agent.tools.files import BinaryFileError, PathEscapeError, _resolve, read_file, str_replace, write_file
from agent.tools.sandbox import run_shell_sandboxed
from agent.tools.tool_errors import tool_errors_to_text
from agent.tools.shell import ShellTimeout
from agent.tools.vision import describe_image_bytes

# Below deepagents' own ~20K-token offload trigger (roughly 70-80K chars) --
# deliberately much lower, since moderate-sized content compounding across
# many rounds is a bigger real cost driver than one single giant result.
# Catching it earlier is the point.
OFFLOAD_THRESHOLD_CHARS = 15_000
# Source-file reads get a much higher inline cap than bash output. A large
# source file (tens of thousands of lines) that can never arrive whole under
# a low cap forces the model to page through it in small windows -- dozens
# of read calls on one file, while summarization keeps evicting the pages it
# just read, a self-feeding read/summarize loop that burns the pass. One
# large read (tens of thousands of tokens) is an order of magnitude cheaper
# than re-sending a growing context across 100+ paging turns. Bash keeps the
# low cap: shell output compounding across rounds is the real cost driver
# there, and no one needs tens of thousands of chars of test output inline.
# 200K chars comfortably covers every hand-written source file in this
# system's real deployments; only generated build output goes larger, and
# that should never be read inline anyway. Accuracy beats token thrift here:
# one large read of a big file costs a small fraction of a cent and makes
# every subsequent edit anchor exact.
READ_INLINE_CAP_CHARS = 200_000
# Generous but bounded -- protects against truly gigantic files while still
# reading enough to make an accurate offload decision.
READ_HARD_CAP_CHARS = 2_000_000


async def _offload_if_large(backend: BackendProtocol | None, tool_name: str, content: str) -> str:
    if backend is None or len(content) <= OFFLOAD_THRESHOLD_CHARS:
        return content
    path = f"/tool_outputs/{tool_name}-{uuid.uuid4().hex[:12]}.txt"
    await backend.awrite(path, content)
    preview = _create_content_preview(content)
    return (
        f"Tool result too large ({len(content)} chars) -- saved to {path}. "
        f"Read it with your read_file tool (use offset/limit to page through it; do not re-read it all at once).\n\n"
        f"Preview (head and tail):\n{preview}"
    )


def make_agent_tools(
    repo_root: str,
    backend: BackendProtocol | None = None,
    initial_last_failed_edit: str | None = None,
) -> tuple[list, dict]:
    """Returns ([bash, read, write, edit, describe_image], last_failed_edit_state)
    -- the agent's tools for reaching the real repo, scoped to `repo_root`,
    plus a mutable dict tracking the edit-repeat guard's current state (see
    the `edit` tool below). `backend` is optional so this stays usable in
    contexts without one -- offloading simply doesn't trigger without it.

    `initial_last_failed_edit` seeds the guard from a prior call's final
    state (a JSON string, or None): this function is called fresh on every
    single work_node invocation (every verify_and_ship loop-back builds a
    brand new deep agent), so a guard scoped only to this function's own
    closure would reset to empty every pass -- unable to catch a model that
    retries the identical failing edit across several separate passes rather
    than within one. The caller (work_node) is responsible for round-tripping
    this through AgentState's own `last_failed_edit_signature` field, the
    same persistence mechanism already used for `committed_sha`/
    `no_diff_streak` across passes.
    """

    def _repo_path(path: str) -> str:
        """Accept the /workspace-prefixed spelling of a repo path. `bash`
        genuinely shows the repo at /workspace, and the system prompt says
        "/workspace IS the repo root" -- so a model calling read/write/edit
        with a /workspace-prefixed path is following that framing
        correctly, and the path-escape guard would otherwise reject it while
        naming the host sandbox path, directly contradicting what the agent
        was told. The /workspace/X -> X mapping is unambiguous, so accept it
        outright rather than relying on prompt wording alone to prevent it.
        """
        if path == "/workspace":
            return "."
        if path.startswith("/workspace/"):
            return path[len("/workspace/"):]
        return path

    @tool
    @tool_errors_to_text
    async def describe_image(path: str, question: str = "") -> str:
        """Look at an image file (screenshot, photo, diagram) attached to this
        task and get a detailed description of it. `path` is repo-relative
        (e.g. ".uploads/ab12cd34/screenshot.png"). Pass `question` to ask
        something specific ("what error message is shown?", "what color is
        the banner?") -- otherwise you get a thorough general description
        including any visible text. Your own model cannot see images; this
        tool is how attached images become usable."""
        # _resolve (same guard read/write/edit use), NOT os.path.join: join
        # DISCARDS repo_root entirely when handed an absolute path, so
        # describe_image("/home/3d-agent/.env") read that file straight off
        # the host and shipped its contents to the vision model. `..` walked
        # out just as easily. This tool runs host-side (unlike bash, which is
        # containerised), so there was no second boundary behind it.
        try:
            resolved = _resolve(repo_root, _repo_path(path))
        except PathEscapeError as e:
            return f"ERROR: {e}"
        real = str(resolved)
        if not os.path.isfile(real):
            return f"ERROR: image not found at {path!r}"
        if os.path.getsize(real) > 20 * 1024 * 1024:
            return "ERROR: image exceeds 20MB"
        # No "or image/png" default: an extensionless file (a key, a .env, a
        # dotfile) guesses to None, and defaulting that to an image mime made
        # the check below pass for exactly the files worth stealing. Unknown
        # type now means refused.
        mime = mimetypes.guess_type(real)[0]
        if not mime or not mime.startswith("image/"):
            return f"ERROR: {path!r} is not a recognised image file ({mime or 'unknown type'})"
        # Routed through ChatOpenAI (not a raw httpx POST) so this call
        # participates in LangSmith tracing like every other agent-* pinned
        # role -- a bare httpx call to litellm's REST endpoint is invisible
        # to LangSmith regardless of call volume, which is why vision usage
        # never showed up in the model-usage-by-role dashboard before.
        try:
            return await describe_image_bytes(open(real, "rb").read(), mime, question)
        except Exception as e:  # noqa: BLE001 -- surfaced to the calling agent as a tool result, not raised
            return f"ERROR: vision call failed: {e}"

    @tool
    @tool_errors_to_text
    async def bash(command: str, timeout: int = 120) -> str:  # noqa: bash timeout clamped below (audit H-18)
        """Run a shell command. Working directory is /workspace, which IS the
        repo root -- `pwd` shows /workspace, paths there map 1:1 to what
        `read`/`write`/`edit` expect (e.g. /workspace/src/App.tsx here ==
        "src/App.tsx" for those tools). Runs inside an isolated sandbox
        container -- only this repo's own files are visible, nothing else on
        the host. This is a DIFFERENT filesystem from your built-in
        ls/read_file/write_file/edit_file/glob/grep tools, which only see
        this agent's own memory/skills paths, never the real repo -- use
        THIS tool (or read/write/edit) for anything repo-related.
        Non-interactive: CI=true is set, there is no stdin, and prompts that
        would otherwise hang get EOF immediately."""
        try:
            # Sandboxed (agent/tools/sandbox.py), not a raw host shell -- this
            # is the only tool that lets the model run an arbitrary command
            # string (read/write/edit already path-guard themselves to
            # repo_root via agent/tools/files.py, and run_checks/git
            # operations use fixed commands our own code chooses, never
            # model-supplied text), so it's the one place a context-injected
            # or badly-confused agent could otherwise reach outside the
            # intended repo entirely -- another project's live secrets, this
            # project's own .env, etc. Container isolation keeps none of that
            # visible from inside the sandbox.
            # audit H-18: the model supplies `timeout`; clamp it to a hard
            # ceiling. bash holds the per-project lock for its whole run, so an
            # unbounded value (or a nonsense one) lets a single confused turn
            # wedge the project for everyone else. 600s is well above any
            # legitimate build/test step.
            _BASH_TIMEOUT_CEILING = 600
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                timeout = 120
            if timeout <= 0:
                timeout = 120
            timeout = min(timeout, _BASH_TIMEOUT_CEILING)
            r = await run_shell_sandboxed(command, repo_root, timeout=timeout)
            content = f"exit_code={r['exit_code']}\n{r['output']}"
            return await _offload_if_large(backend, "bash", content)
        except ShellTimeout as e:
            return f"TIMED OUT: {e}"

    @tool
    @tool_errors_to_text
    async def read(path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a repo file (the real codebase -- NOT this agent's own
        memory/skills, use your built-in read_file for those). Path is
        RELATIVE to the repo root, e.g. "src/App.tsx" -- same root `bash`'s
        /workspace maps to, just without the /workspace/ prefix.

        For LARGE files, page through THIS tool directly: `offset` is the
        1-based line to start from, `limit` the number of lines (e.g.
        offset=200, limit=120). A full read of a large file returns only a
        head/tail preview plus its line count -- follow up with offset/limit
        slices, don't re-request the whole file."""
        # offset/limit paging is supported directly on this tool specifically
        # because a model's instinct on a too-large result is to call this
        # tool again -- supporting that instinct beats correcting it. Without
        # paging support, a retry would just re-offload the entire file to a
        # brand-new pointer and loop.
        try:
            content = read_file(repo_root, _repo_path(path), max_chars=READ_HARD_CAP_CHARS)
        except (FileNotFoundError, PathEscapeError, BinaryFileError) as e:
            return f"ERROR: {e}"
        except IsADirectoryError:
            return f"ERROR: {path!r} is a directory, not a file -- use `bash ls` to see what's in it"
        except NotADirectoryError:
            # A worktree's `.git` is a pointer FILE, so `.git/HEAD` and friends
            # resolve THROUGH a file rather than into a directory. Uncaught,
            # this took down a whole run (see agent/tools/tool_errors.py).
            return (
                f"ERROR: {path!r} cannot be read -- a directory in that path is actually a file "
                f"(this repo is a git worktree, so .git is a pointer file; use `bash git ...` for "
                f"anything you were hoping to learn from under .git/)"
            )
        except OSError as e:
            # Never f"{e}" for an OSError here: its str() carries the absolute
            # host sandbox path, which files.py deliberately keeps from the model.
            return f"ERROR: cannot read {path!r}: {e.strerror or e}"
        if offset or limit:
            lines = content.split("\n")
            start = max(0, (offset or 1) - 1)
            count = limit if limit and limit > 0 else 200
            slice_lines = lines[start:start + count]
            if not slice_lines:
                return f"(no lines at offset {offset} -- the file has {len(lines)} lines)"
            numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(slice_lines))
            remaining = len(lines) - (start + len(slice_lines))
            footer = f"\n\n[lines {start + 1}-{start + len(slice_lines)} of {len(lines)}" + (
                f"; {remaining} more after this]" if remaining > 0 else "]"
            )
            return numbered + footer
        if len(content) > READ_INLINE_CAP_CHARS:
            lines = content.split("\n")
            preview = _create_content_preview(content)
            return (
                f"File is large ({len(content)} chars, {len(lines)} lines) -- showing a preview. "
                f"Call read again with offset/limit to page through the part you need, "
                f"using BIG windows (e.g. read(path, offset=1, limit=800)) -- do not "
                f"page in small 50-100 line slices, that loops.\n\nPreview (head and tail):\n{preview}"
            )
        return content

    # A better error message alone (str_replace's own whitespace-mismatch
    # diagnosis) is not always enough to stop a model from resubmitting the
    # exact same (path, old_string, new_string) edit call after it already
    # failed. This is not a probabilistic risk to mitigate with a better
    # prompt -- str_replace is a pure function of the file's current
    # content, so an identical signature that already failed is guaranteed
    # to fail identically again unless the file changed in between. Refusing
    # the exact repeat outright (rather than re-running str_replace and
    # hoping the model reads the response this time) is a hard, deterministic
    # backstop, matching this project's own standing preference for
    # code-enforced guarantees over trusting instruction-following alone.
    # Signature is a JSON string (not a plain tuple) so it round-trips
    # cleanly through AgentState's checkpointed, serialized storage across
    # passes -- see this function's own docstring on why a single pass's
    # closure isn't enough.
    _last_failed_edit: dict = {"signature": initial_last_failed_edit}

    @tool
    @tool_errors_to_text
    def write(path: str, content: str) -> str:
        """Write (create or overwrite) a repo file (the real codebase -- NOT
        this agent's own memory/skills, use your built-in write_file for
        those). Path is RELATIVE to the repo root, same convention as `read`."""
        try:
            write_file(repo_root, _repo_path(path), content)
            _last_failed_edit["signature"] = None  # this path's content may have just changed
            return "OK"
        except PathEscapeError as e:
            return f"ERROR: {e}"
        except IsADirectoryError:
            return f"ERROR: {path!r} is an existing directory -- pick a file path"
        except NotADirectoryError:
            return f"ERROR: cannot write {path!r} -- a directory in that path is actually a file"
        except OSError as e:
            return f"ERROR: cannot write {path!r}: {e.strerror or e}"

    @tool
    @tool_errors_to_text
    def edit(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact, unique substring in a repo file (the real
        codebase -- NOT this agent's own memory/skills, use your built-in
        edit_file for those). Path is RELATIVE to the repo root, same
        convention as `read`. old_string must match exactly once.
        Resubmitting the IDENTICAL (path, old_string, new_string) after it
        already failed will be REFUSED without retrying -- the file hasn't
        changed, so it cannot succeed; re-read the target lines first. This
        is enforced across the whole task, not just this one turn."""
        signature = json.dumps([path, old_string, new_string])
        if _last_failed_edit.get("signature") == signature:
            return (
                "ERROR: REFUSED without retrying -- this exact edit (same path, old_string, and "
                "new_string) already failed and you just resubmitted it completely unchanged (this is "
                "enforced across the whole task, not just this turn -- it doesn't matter if this looks "
                "like a fresh attempt to you). str_replace is a pure function of the file's CURRENT "
                "content: if old_string didn't match last time, it cannot match now unless something "
                "else changed that file in between, which nothing has. Re-read the exact target lines "
                "first (e.g. `bash cat -A <path>` to see whitespace explicitly) and submit a corrected "
                "old_string -- resubmitting this one again will keep hitting this same refusal, not "
                "str_replace's error."
            )
        try:
            str_replace(repo_root, _repo_path(path), old_string, new_string)
            _last_failed_edit["signature"] = None
            return "OK"
        except (FileNotFoundError, PathEscapeError, ValueError, BinaryFileError) as e:
            _last_failed_edit["signature"] = signature
            return f"ERROR: {e}"
        except OSError as e:
            _last_failed_edit["signature"] = signature
            return f"ERROR: cannot edit {path!r}: {e.strerror or e}"

    return [bash, read, write, edit, describe_image], _last_failed_edit
