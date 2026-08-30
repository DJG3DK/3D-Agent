"""build_deep_agent -- per-project, per-task factory for the deepagents-based
work engine. Returns a CompiledStateGraph, invoked manually (not nested as a
native subgraph -- the outer AgentState and this graph's own state share no
keys) by the outer "work" node.
"""

import json

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.store.base import BaseStore

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from agent.config import Config, PROJECTS
from agent import runtime_settings as _rs
from agent.middleware.hidden_tools import HiddenToolsMiddleware
from agent.middleware.budget_guard import BudgetMeterCallback, BudgetGuardMiddleware, BudgetTracker
from agent.middleware.model_pin import PlanCodeModelMiddleware
from agent.tools.agent_tools import make_agent_tools
from agent.tools.project_db import make_project_db_tool
from agent.tools.checks import run_all_checks

# Three-tier memory:
#
#   /memories/AGENTS.md   -- semantic, per-project, agent-writable during
#                            normal work (durable facts about this repo).
#   /org-memory/AGENTS.md -- semantic, cross-project, read-only to every task
#                            agent (enforced via `permissions`, not just
#                            prompting -- this system doesn't trust
#                            instruction-following alone for anything that
#                            matters). Populated by application code only:
#                            one task's agent should never be able to alter
#                            what every other project's agent believes.
#   /episodes/{repo}/...  -- structured records of past task outcomes
#                           (goal, result, cost, escalation reason if any),
#                           written by verify_and_ship at terminal state, not
#                           auto-loaded into every task's context (that would
#                           defeat the point of keeping the hot path small).
#                           Read only by the consolidation agent
#                           (agent/consolidation.py), which distills them
#                           into /memories/AGENTS.md updates on a schedule --
#                           background consolidation, not the agent editing
#                           its own memory ad hoc as the only mechanism.
MEMORY_PATH = "/memories/AGENTS.md"
ORG_MEMORY_PATH = "/org-memory/AGENTS.md"
ORG_NAMESPACE = ("org",)
EPISODES_ROUTE = "/episodes/"

# Skills -- deepagents' progressive-disclosure mechanism, distinct from
# memory: memory is small, durable, always-relevant facts (loaded in full,
# every task); skills are large, situational domain knowledge (a subsystem's
# real architecture, a non-obvious integration's rules) that would bloat
# every task's context if treated as memory, so only a one-line
# name+description loads by default -- the agent reads a skill's full
# SKILL.md via its own read_file tool only when a task actually touches that
# area. Per-project namespace (skills are repo-specific domain knowledge,
# not cross-project policy like org-memory). Read-only to the agent, same
# reasoning as org-memory: skills are curated reference material, updated
# deliberately (seed_skill), not something that should drift from an
# agent's own mid-task edits.
SKILLS_ROUTE = "/skills/"
SKILLS_MANIFEST_PATH = "/skills/_manifest.json"

# Real per-call cost via BudgetGuardMiddleware makes SummarizationMiddleware's
# own trigger about context quality, not cost -- the budget ceiling already
# owns cost. Trigger well before a model's real context limit so summarization
# is a normal, unremarkable event, not a last-resort save. `keep` mirrors the
# library default (last 20 messages kept verbatim after summarizing).
# Tuned alongside READ_INLINE_CAP_CHARS (agent_tools.py): a whole large source
# file can arrive inline, so the trigger needs enough headroom to admit one
# such read without churning the summarizer immediately afterward. Every
# pinned model has >=262K context, so trading some headroom for stability is
# cheap -- context quality owns this trigger, cost never did (BudgetGuard
# owns cost).
SUMMARIZATION_TRIGGER = [("tokens", 80_000), ("messages", 120)]  # OR semantics (list of clauses)
# Retention is expressed in TOKENS, deliberately matching the unit the trigger
# above uses. It was ("messages", 20), and a message-count keep against a
# token-count trigger is a unit mismatch with no relationship between the two:
# nothing bounds how much a summarization actually reclaims, because 20
# messages can be 8k tokens or 70k depending purely on how big the recent tool
# results happen to be.
#
# That is not theoretical. Measured on a real planning session (2026-08-27) at
# the exact state that fired summarization: 28 messages / 73_068 tokens, of
# which the six largest were ~10k tokens each (inline file reads). Keeping 20
# messages preserved 61_695 tokens -- it reclaimed 11k of 73k, 15%, and left
# 18k of headroom under an 80k trigger. Roughly two more file reads and it
# fired again. The session's observed cycle was: summarize, 2-3 tool calls,
# summarize, 2-3 tool calls. Two summarizations inside twelve minutes, both
# doing real work and neither buying meaningful room.
#
# The degenerate end of that mismatch is worse than churn: if the kept window
# ever exceeds the trigger on its own, summarization fires before EVERY model
# call and can never get back under, so the conversation stops making progress
# while still paying for a summarizer call each turn.
#
# 30k against an 80k trigger guarantees >=50k reclaimed every time (measured on
# the same state: 12 messages / 26_163 tokens kept, 53_837 of headroom -- about
# eight large reads instead of two). It also lowers the average context a
# planning call carries, which is where the real money goes: kimi-k3 input is
# $3/M, so every 10k tokens of avoided context is ~$0.03 off every single call
# in the conversation.
#
# Caveat worth knowing: the library keeps at least one message, so a SINGLE
# message larger than this budget still lands above it (the binary search in
# _find_token_based_cutoff cannot cut inside a message). Planning reads are
# capped at 40k chars (~12k tokens) so they cannot breach it; agent_tools.read
# offloads above READ_INLINE_CAP_CHARS for the same reason.
SUMMARIZATION_KEEP = ("tokens", 30_000)
# The library default (4000) is tuned for ordinary back-and-forth chat, where a
# HumanMessage recurs often. Our conversations are tool-call-heavy: one
# HumanMessage with the goal, then dozens of AIMessage/ToolMessage pairs before
# the next one. SummarizationMiddleware's own trim step (trim_messages with
# strategy="last", start_on="human") requires a HumanMessage to anchor the
# trimmed batch -- with a small token budget, a batch that's mostly tool pairs
# can have no HumanMessage in that window, so trim_messages returns an empty
# list. The library's own fallback for that case is to silently return the
# literal string "Previous conversation was too long to summarize." as the
# entire prior context, with no retry and no error -- confirmed to wipe the
# goal and prior findings from context on a real task.
#
# A bigger trim budget doesn't fix this: a regression test
# (tests/test_summarization_trim.py) confirms that raising it still produces
# an empty trim on the same failure shape, because the sole HumanMessage can
# sit further back than any finite budget reaches if the tool-heavy stretch
# after it is long enough. The trim step exists to bound the summarizer
# call's own input size -- but SUMMARIZATION_TRIGGER already bounds the
# untrimmed batch to roughly its own threshold (the OR trigger fires the
# instant either clause is met, so total tokens at fire time can't run away
# past that), comfortably inside any modern model's context window. So the
# trim step is redundant for our shape and only adds a failure mode --
# disable it outright (None skips trimming entirely) rather than trying to
# out-guess a budget that has no safe value for an unbounded-distance-to-
# last-HumanMessage conversation shape.
SUMMARIZATION_TRIM_TOKENS = None

# Defense-in-depth against a runaway loop that BudgetGuardMiddleware alone
# wouldn't catch -- a stuck loop making many near-zero-cost calls (routed to
# a cheap model tier) could burn enormous wall-clock/API-call-count before
# ever crossing the dollar ceiling. Deliberately generous (well above any
# normal task's real call count) so this never fires on legitimate work,
# only genuine runaway pathology. `run_limit` (per single astream_events()
# invocation, i.e. per outer "work" pass), not `thread_limit` -- a
# thread_limit would persist across every resume of a long task's whole
# lifetime, which doesn't map onto anything meaningful here the way it would
# for a genuinely single-shot agent. exit_behavior="error" (not "end"/
# "continue") so this surfaces as a real exception routed through
# work_node's existing generic `except Exception` handler -> a clear
# escalation, not a silently-truncated response that could get misread as a
# normal completion.
# The numbers themselves now live in runtime_settings (Settings -> Runtime
# limits) so they can be retuned without an edit and a restart; the reasoning
# above is why they are shaped this way, and still applies. Read via
# _rs.as_int("model_call_run_limit") / ("tool_call_run_limit") at the point of
# use -- deliberately not re-declared here as constants, because a constant
# that no longer drives anything is a trap for the next person to edit it.

# Human-in-the-loop pre-execution approval gate. This system's only other
# safety layers are the budget ceiling and the post-hoc review-service gate
# (after code already ran). deepagents' own HumanInTheLoopMiddleware (wired
# in below via create_deep_agent's `interrupt_on=` param, backed by
# LangGraph's native interrupt()/Command(resume=...)) pauses before a
# specific tool call executes, when it matches a `when` predicate here.
#
# Deliberately narrow, not "approve every tool call" (that would make this
# system unusable -- a real task makes dozens of tool calls). Scoped to
# what the sandboxing fix (agent/tools/sandbox.py) does not already cover:
# Docker isolation bounds `bash`'s blast radius to the repo checkout, but
# genuinely sensitive files (a project's own auth/config, .env, .git
# internals, CI/deploy config) live inside that same checkout -- the sandbox
# boundary doesn't protect a repo from itself. Also gates recognizably
# destructive git/shell patterns (force-push, rm -rf, sudo) regardless of
# path, since those are dangerous even scoped to one repo.
#
# Applied to the coordinator and both subagents (below) -- not just the
# coordinator -- because investigator, despite its own system prompt's
# "read-only" framing, is given the full `bash` tool (needed for real
# find/grep-across-the-tree exploration; there's no separate read-only-
# shell primitive in this system yet) and could otherwise run something
# destructive with zero gate at all. Two decisions only (approve/reject),
# not edit/respond -- keeps the operator-facing payload and the dashboard
# UI simple; a rejected call gets a clear message back to the model instead
# of silently vanishing.
_SENSITIVE_PATH_MARKERS = (
    "config/", "auth.json", ".env", ".git/", "secret", "credential",
    "deploy", ".github/workflows", ".ssh",
    # audit C-2: the check runner executes `npm run <script>` and the reviewer
    # runs install lifecycle scripts, all defined in these files -- which the
    # agent can rewrite with its own `write`/`edit` tool. Until now that write
    # was ungated (matched no marker), so a rewritten test/build/install script
    # was an unreviewed path to host code execution. Editing any of them now
    # requires operator approval in strict mode, the same as touching a .env.
    # Lowercase (both predicates lowercase before matching).
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pnpm-workspace.yaml", "vitest.config", "jest.config", "playwright.config",
    "tsconfig", ".npmrc", "makefile", "dockerfile",
)
# MUST be lowercase: both predicates below lowercase the command before
# matching, so an uppercase character in a marker can never match anything.
# "chmod -R"/"chown -R" were written with a capital R and therefore silently
# never gated a single recursive chmod/chown from the day they were added --
# found 2026-08-24 by the auto-approve gate's own tests. Keep this list
# lowercase, and let tests/test_auto_approve_gate.py catch it if that slips.
_DANGEROUS_COMMAND_MARKERS = (
    # "> /dev/" removed from this substring list -- the regex below owns that
    # case now, because a substring cannot express the needed exception.
    "rm -rf", "git push", "sudo ", "chmod -r", "chown -r", ":(){ ",
)


# audit M-7: the substring matchers below were trivially evadable -- `rm  -rf`
# (two spaces), `rm -fr` (flag order), a tab instead of a space all slipped past.
# The list can't be made sound by extension, and it no longer needs to be: C-2
# made the Docker sandbox the real boundary (bash runs in an isolated container
# that cannot see the host, other repos, or any secret, and whose only mutable
# state is a throwaway worktree copy). This gate is now a footgun safety-net --
# it catches the obvious destructive command so an operator gets a confirm
# prompt -- not the security boundary. Normalizing whitespace and matching a few
# regexes for flag-order variants makes the net catch the demonstrated evasions.
_WS_RUN = __import__("re").compile(r"\s+")


def _normalize_command(command: str) -> str:
    return _WS_RUN.sub(" ", command.lower()).strip()


import re as _re
_DANGEROUS_COMMAND_PATTERNS = (
    _re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"),  # rm -rf / -fr / -Rf ...
    _re.compile(r"\bgit\b.*\bpush\b.*(--force|-f\b|\+)"),  # force push (any -c ... prefix)
    _re.compile(r"\bsudo\b"),
    _re.compile(r"\bchmod\s+-[a-z]*r|\bchown\s+-[a-z]*r"),  # recursive chmod/chown
    # Writing to a raw DEVICE (> /dev/sda) is destructive. Redirecting to the
    # null/stream devices is not -- and `2>/dev/null` is the single most
    # common idiom in shell, so matching it made EVERY quiet command prompt
    # for approval, auto-approve mode included (live 2026-08-27: a build's
    # plain `ls ... 2>/dev/null` and `find ... 2>/dev/null` both gated as
    # "destructive"; the operator asked why auto-approve wasn't working).
    _re.compile(r">\s*/dev/(?!null\b|zero\b|stdout\b|stderr\b|tty\b|fd/)"),
    # dd writing to a raw device -- was never caught (no `>` involved), found
    # while fixing the redirect pattern above.
    _re.compile(r"\bof=/dev/(?!null\b|zero\b)"),
    _re.compile(r":\(\)\s*\{"),  # fork bomb
    _re.compile(r"\bfind\b.*-delete"),
)

# `ln -s`, `ln --symbolic`, and the same via env/xargs. Word-boundary anchored
# so "align -symbolic" or a filename containing "ln -s" does not trip it.
_SYMLINK_RE = _re.compile(r"(^|[;&|]\s*|\s)ln\s+(-[a-zA-Z]*s[a-zA-Z]*|--symbolic)\b")


def _matches_dangerous(command: str) -> bool:
    norm = _normalize_command(command)
    if any(marker in norm for marker in _DANGEROUS_COMMAND_MARKERS):
        return True
    return any(p.search(norm) for p in _DANGEROUS_COMMAND_PATTERNS)


def _bash_needs_approval(req) -> bool:
    raw = str(req.tool_call["args"].get("command", ""))
    norm = _normalize_command(raw)
    if any(marker in norm for marker in _SENSITIVE_PATH_MARKERS):
        return True
    if _bash_creates_a_symlink(norm):
        return True
    return _matches_dangerous(raw)


def _bash_creates_a_symlink(command: str) -> bool:
    """audit C2/H2: `ln -s <anything> node_modules` inside the worktree decides
    what the HOST bind-mounts into the next container. sandbox.py now refuses
    targets outside the project's own paths, so this is defence in depth --
    but a symlink whose target the agent chose is worth surfacing, and it
    matched no marker before."""
    return bool(_SYMLINK_RE.search(command))


def _bash_is_destructive(req) -> bool:
    """The subset of `bash` that stays gated even in auto-approve mode.

    Deliberately NOT the same predicate as _bash_needs_approval: the two
    marker lists guard genuinely different risks. A sensitive PATH match
    (reading .env, editing .github/workflows) is about touching something
    that deserves a second look, and it's the case that fires constantly on
    ordinary work -- that's the noise auto-approve exists to remove. A
    DESTRUCTIVE COMMAND match (rm -rf, a force push, sudo, a fork bomb) is
    about an action that can't be undone by reverting a diff, and no
    per-user preference should be able to wave that through unattended.
    """
    return _matches_dangerous(str(req.tool_call["args"].get("command", "")))


def _writes_a_git_dir(path: str) -> bool:
    """A write that lands the worktree's `.git` pointer, or anything that looks
    like a git dir or a hook.

    audit H2: the sensitive-path list matches ".git/" WITH the slash, so it
    caught `.git/hooks/pre-commit` but not the bare pointer FILE `.git` --
    and rewriting that pointer is what re-aims every host-side git command at
    an agent-controlled git dir (see agent/tools/git.py). Hooks are disabled
    unconditionally there now, so this is the second lock rather than the
    only one, but the write itself still deserves a prompt.
    """
    norm = path.replace("\\", "/").lower().rstrip("/")
    base = norm.rsplit("/", 1)[-1]
    if base == ".git":                      # the pointer file itself
        return True
    if "/hooks/" in norm or base == "hooks":  # any hooks directory
        return True
    # a bare git dir the agent is assembling under another name
    return base in ("head", "config") and "/objects/" not in norm and (
        norm.endswith("/head") and "/.git/" not in norm)


def _file_op_needs_approval(req) -> bool:
    args = req.tool_call["args"]
    path = str(args.get("path", "")).lower()
    if any(marker in path for marker in _SENSITIVE_PATH_MARKERS):
        return True
    return _writes_a_git_dir(path)


def _describe_bash(tool_call, state, runtime) -> str:
    return f"Approval needed: run this shell command?\n\n{tool_call['args'].get('command', '')}"


def _describe_file_op(tool_call, state, runtime) -> str:
    name = tool_call["name"]
    path = tool_call["args"].get("path", "")
    return f"Approval needed: {name} a sensitive-looking path?\n\npath: {path}"


def _describe_ask_user(tool_call, state, runtime) -> str:
    # Same (tool_call, state, runtime) signature as _describe_bash -- a bare
    # single-arg lambda here crashes the work node the moment ask_user fires.
    args = tool_call.get("args", {}) or {}
    text = "QUESTION FROM THE AGENT:\n" + str(args.get("question", ""))
    if args.get("options"):
        text += "\n\nOptions:\n" + str(args["options"])
    return text


def _make_ask_user_tool():
    """ask_user -- the agent's clarification channel to the operator.

    Exists so an ambiguous goal (two reasonable readings that lead to
    materially different work) gets resolved by asking rather than guessed
    silently. The tool never executes: the HITL middleware interrupts it and
    the operator's typed answer becomes the ToolMessage via the native
    `respond` decision -- the library's own documented "ask user"-style-tool
    pattern.
    """

    @tool
    def ask_user(question: str, options: str = "") -> str:
        """Ask the OPERATOR one focused clarifying question and wait for
        their answer. Use BEFORE starting large work when the goal has a
        consequential ambiguity -- two reasonable readings that lead to
        materially different implementations. Give concrete options when
        they exist (e.g. "A) paginate the list  B) optimize image caching").
        Do NOT use for anything you can verify yourself in the repo, and do
        not ask more than one question at a time. The tool result is the
        operator's own reply -- treat it as authoritative direction."""
        return (
            "(no operator response captured -- proceed with your best "
            "judgment and state the assumption you are making)"
        )

    return ask_user


INTERRUPT_ON = {
    "ask_user": {
        "allowed_decisions": ["respond"],
        # No `when` predicate: every call interrupts -- the whole point is
        # a human answer; the tool body is only a fallback if something
        # ever bypasses the interrupt.
        "description": _describe_ask_user,
    },
    "bash": {
        "allowed_decisions": ["approve", "reject"],
        "when": _bash_needs_approval,
        "description": _describe_bash,
    },
    "write": {
        "allowed_decisions": ["approve", "reject"],
        "when": _file_op_needs_approval,
        "description": _describe_file_op,
    },
    "edit": {
        "allowed_decisions": ["approve", "reject"],
        "when": _file_op_needs_approval,
        "description": _describe_file_op,
    },
}

# Auto-approve mode (per-user opt-in, User.auto_approve_commands, set from
# the dashboard's Settings tab). Removes the approval prompt for the
# sensitive-PATH class -- reading a config file, editing a workflow -- which
# is the case that fires constantly during ordinary work and is what makes
# a long task need babysitting.
#
# What it deliberately does NOT remove:
#   * destructive commands (_bash_is_destructive) stay gated, always. The
#     whole point of that list is actions a revert can't undo, and a
#     preference toggle is the wrong instrument for switching those off.
#   * ask_user stays interrupting. It's the agent's question channel, not a
#     safety gate -- auto-approving it would just feed every clarifying
#     question the generic "use your best judgment" fallback instead of the
#     operator's real answer, which makes the agent worse, not faster.
#
# This grants no capability the operator doesn't already have: they could
# approve each of these by hand today. It only removes the clicking.
INTERRUPT_ON_AUTO_APPROVE = {
    "ask_user": INTERRUPT_ON["ask_user"],
    "bash": {
        "allowed_decisions": ["approve", "reject"],
        "when": _bash_is_destructive,
        "description": _describe_bash,
    },
}


def interrupt_on_for(auto_approve_commands: bool) -> dict:
    return INTERRUPT_ON_AUTO_APPROVE if auto_approve_commands else INTERRUPT_ON


def llm_for_role(config: Config, model_name: str, reasoning_effort: str | None = None, timeout: int | None = None, callbacks: list | None = None) -> ChatOpenAI:
    # model_name is a bare LiteLLM profile alias, resolved entirely by the
    # proxy, not by anything in this process.
    #
    # stream_usage=True is not optional: ChatOpenAI only auto-enables it when
    # talking to the default OpenAI base_url/client, which this custom
    # litellm_base_url never matches. Every model call here goes through
    # agent.astream_events(..., version="v3"), so it's invoked as a real
    # token stream rather than a single ainvoke -- and without stream_usage,
    # a streamed OpenAI-compatible response never includes
    # `stream_options: {"include_usage": true}`, so
    # response_metadata["token_usage"] comes back empty on every call.
    # BudgetGuardMiddleware's cost read falls back to 0.0 in that case (by
    # design, to fail loud-ish rather than guess), which would mean the one
    # non-negotiable hard dollar ceiling in this whole system silently never
    # trips.
    #
    # reasoning_effort (None by default -- opt-in per call site, not a
    # blanket default): confirmed live that OpenRouter's Gemini models
    # accept this directly on ChatOpenAI and genuinely spend extra "thinking"
    # tokens on it (response.usage_metadata.output_token_details.reasoning
    # comes back > 0, and it's billed/counted like any other output token --
    # BudgetGuardMiddleware sees it same as always). Not every pinned role's
    # model necessarily supports this OpenRouter parameter; only pass it for
    # a role/model combination confirmed to actually use it.
    #
    # timeout (180 by default, overridable per call site): confirmed live
    # 2026-08-23 that agent-planning-chat (gemini-3.7-flash) called with
    # reasoning_effort="high" routinely blows past 180s -- OpenRouter itself
    # aborts the still-in-flight call ("OpenrouterException - The operation
    # was aborted"), which litellm then surfaces to this client as an HTTP
    # 400, which openai's SDK in turn raises as BadRequestError. A "high"
    # reasoning budget on a planning turn isn't on the same latency budget as
    # an interactive coordinator call, so a role that opts into high
    # reasoning_effort should also opt into a longer timeout at its own call
    # site rather than eating spurious aborts on real, in-progress work.
    return ChatOpenAI(
        model=model_name,
        base_url=config.litellm_base_url,
        api_key=config.litellm_api_key,
        temperature=0,
        timeout=timeout if timeout is not None else _rs.as_int("model_call_timeout_s"),
        stream_usage=True,
        reasoning_effort=reasoning_effort,
        # callbacks: how a model invoked OUTSIDE the graph's model node still
        # gets metered -- SummarizationMiddleware ainvoke()s its summary model
        # directly, where no agent middleware wraps the call, so the summarizer
        # role attaches a BudgetMeterCallback here (see budget_guard.py).
        callbacks=callbacks,
    )


def project_namespace(repo: str):
    # One shared, cross-task memory file per project -- every task/thread for
    # this repo reads and writes the same store-backed AGENTS.md, not a
    # per-conversation copy. `rt` (Runtime) is unused here since the
    # namespace is fully determined by which project this factory call is
    # for, not by anything request-time.
    def namespace(rt):
        return (repo,)

    return namespace


def org_namespace(rt):
    return ORG_NAMESPACE


def episodes_namespace(repo: str):
    def namespace(rt):
        return ("episodes", repo)

    return namespace


def skills_namespace(repo: str):
    def namespace(rt):
        return ("skills", repo)

    return namespace


def build_memory_backend(repo: str, store: BaseStore) -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend(),  # ephemeral, thread-scoped scratch for everything else
        routes={
            "/memories/": StoreBackend(namespace=project_namespace(repo), store=store),
            "/org-memory/": StoreBackend(namespace=org_namespace, store=store),
            EPISODES_ROUTE: StoreBackend(namespace=episodes_namespace(repo), store=store),
            SKILLS_ROUTE: StoreBackend(namespace=skills_namespace(repo), store=store),
        },
    )


def route_local_path(route: str, path: str) -> str:
    """The key a CompositeBackend route's StoreBackend actually stores `path`
    under: the route prefix is stripped down to a leading slash before the
    path reaches the route's backend (a composite aread of
    "/memories/AGENTS.md" errors with "File '/AGENTS.md' not found").
    Every piece of app code that touches an agent-visible file via a bare
    StoreBackend (seeding, the system-prompt reads, consolidation) must use
    this stripped form, or it reads/writes a key the agent's own file tools
    can never see.
    """
    assert path.startswith(route), f"{path!r} is not under route {route!r}"
    return "/" + path[len(route):]


async def _seed_if_absent(backend: StoreBackend, path: str, content: str) -> None:
    existing = await backend.aread(path)
    if existing.error is None:
        return
    await backend.awrite(path, content)


async def seed_memory(repo: str, store: BaseStore, content: str) -> None:
    """Writes the initial AGENTS.md content for a project if nothing is
    there yet -- idempotent, safe to call on every server startup. Does not
    overwrite existing content, since the agent is expected to extend this
    file over time and a redeploy shouldn't clobber what it's learned since
    the last seed.
    """
    backend = StoreBackend(namespace=project_namespace(repo), store=store)
    # route_local_path, not MEMORY_PATH -- a bare StoreBackend must use the
    # same stripped key the composite's /memories/ route produces, or the
    # agent's own file tools can never see what was seeded.
    await _seed_if_absent(backend, route_local_path("/memories/", MEMORY_PATH), content)


async def seed_org_memory(store: BaseStore, content: str) -> None:
    """Same idempotent-seed contract as seed_memory, but for the single
    cross-project org-memory file. Application-code-only -- the agent has no
    write access to this path (see the `permissions` rule in
    build_deep_agent), so this is the only way this file is ever updated,
    by design.
    """
    backend = StoreBackend(namespace=org_namespace, store=store)
    await _seed_if_absent(backend, route_local_path("/org-memory/", ORG_MEMORY_PATH), content)


async def seed_skill(repo: str, store: BaseStore, name: str, description: str, content: str) -> None:
    """Writes (or overwrites) one skill's SKILL.md and registers it in the
    project's manifest. Unlike seed_memory/seed_org_memory this is not
    idempotent-skip -- a skill is curated reference material authored
    deliberately (application code, not the agent), so re-running this with
    updated content is the intended way to revise a skill.
    """
    backend = StoreBackend(namespace=skills_namespace(repo), store=store)
    await backend.awrite(route_local_path(SKILLS_ROUTE, f"{SKILLS_ROUTE}{name}/SKILL.md"), content)

    manifest_key = route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH)
    manifest_result = await backend.aread(manifest_key)
    manifest = {}
    if manifest_result.error is None and manifest_result.file_data:
        try:
            manifest = json.loads(file_data_to_string(manifest_result.file_data))
        except (json.JSONDecodeError, TypeError):
            manifest = {}
    manifest[name] = description
    await backend.awrite(manifest_key, json.dumps(manifest, indent=2))


async def load_skills_summary(repo: str, store: BaseStore) -> str:
    """The level-1 progressive-disclosure load: name+description only, for
    every registered skill, formatted for the system prompt. Manual
    workaround for the same AsyncPostgresStore incompatibility documented on
    build_deep_agent (SkillsMiddleware's own startup load calls the same
    broken download_files/adownload_files path) -- deepagents' `skills=`
    parameter is not used here for the same reason `memory=` isn't.
    """
    backend = StoreBackend(namespace=skills_namespace(repo), store=store)
    manifest_result = await backend.aread(route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH))
    if manifest_result.error is not None or not manifest_result.file_data:
        return "(no skills registered for this project)"
    try:
        manifest = json.loads(file_data_to_string(manifest_result.file_data))
    except (json.JSONDecodeError, TypeError):
        return "(no skills registered for this project)"
    if not manifest:
        return "(no skills registered for this project)"
    lines = [
        f"- {name}: {description} (full instructions: read {SKILLS_ROUTE}{name}/SKILL.md)"
        for name, description in manifest.items()
    ]
    return "\n".join(lines)


def _make_run_checks_tool(repo_root: str, repo: str):
    @tool
    async def run_checks() -> str:
        """Run this project's REAL typecheck, lint, and test suite (the
        exact same commands the outer verification gate will run after you
        report done). Call this yourself before claiming a step or the
        whole task is complete -- it's much cheaper and faster than finding
        out from the outer gate that something you thought was fixed
        actually isn't. The outer gate re-runs these checks itself
        regardless of what you report; this tool exists purely so you get
        the feedback sooner, not as a substitute for that gate.
        """
        result = await run_all_checks(repo_root, repo)
        return result["summary"]

    return run_checks


# Shared across all three agents (coordinator + both subagents). This system
# exposes two entirely separate filesystems with no way to tell them apart
# from tool names alone:
#   - deepagents' own native tools (ls/read_file/write_file/edit_file/glob/
#     grep, auto-provided by FilesystemMiddleware, required, can't be
#     removed) -- these only see the virtual CompositeBackend routes
#     (/memories/, /org-memory/, /skills/, /episodes/) and never the real
#     repo, no matter what path is given.
#   - This system's own custom tools (bash/read/write/edit, agent_tools.py)
#     -- these are the only way to reach the real repo. `bash` runs inside a
#     Docker sandbox (agent/tools/sandbox.py) with the repo mounted at
#     /workspace; `read`/`write`/`edit` take paths relative to that same
#     repo root directly (no /workspace/ prefix -- e.g. "frontend/src/App.tsx").
# Without this guidance, a model can burn real iterations discovering the
# distinction the hard way: `ls`/`read_file`/`grep` fail with "No files
# found"/"not found" against real repo paths (they structurally cannot see
# the repo) before it stumbles onto /workspace via bash trial-and-error.
_FILESYSTEM_GUIDANCE = """IMPORTANT -- two separate filesystems, not one:
- Your built-in `ls`/`read_file`/`write_file`/`edit_file` tools ONLY see this \
agent's own memory/skills paths (/memories/, /org-memory/, /skills/, /episodes/) -- NEVER the \
actual repo code, regardless of what path you give them. Use them only for those specific paths. \
There is NO built-in glob/grep: to SEARCH the real repo, use `bash` with rg/grep inside \
/workspace (e.g. `rg -n "someSymbol" src frontend/src`); to find a skills/memory file, read the \
skills manifest or ls the route and read_file the file directly.
- Your `bash`/`read`/`write`/`edit` tools are the ONLY way to reach the real repo. `bash` runs \
inside a sandbox with the repo mounted at /workspace (so `pwd` there shows /workspace, and \
`/workspace` IS the repo root). `read`/`write`/`edit` take paths RELATIVE to that same repo root \
-- e.g. "frontend/src/App.tsx", never "/workspace/frontend/src/App.tsx" and never any other \
absolute host path.
- Concretely: calling `read_file` with `file_path: "/workspace/src/core/app.js"` returns "File not \
found" -- NOT because the file is missing, but because `read_file` can never see the real repo at \
all, so every real-repo path looks "not found" to it. If you see that error on a path you know \
exists, the fix is never to search harder for the file -- it's to switch tools: use `read` (with \
the path made relative, "src/core/app.js") instead of `read_file`. The two tools take different \
parameter names too -- `read_file` wants `file_path`, `read`/`write`/`edit` want `path` -- passing \
the wrong one is also a fast way to hit an avoidable error.
- NEVER run `git commit` (or amend/rebase) yourself via bash. The verify/ship gate commits your \
work for you after its own checks pass -- a self-made commit bypasses that bookkeeping and gets \
absorbed anyway, so it only adds confusion. Just edit files and let the gate handle git."""

INVESTIGATOR_SYSTEM_PROMPT = """You are a read-only investigation subagent. You research and \
report -- you never modify anything. Your tools do not include write/edit (restricted at the code \
level, not just instruction), so don't waste turns trying to change files; focus entirely on \
reading, searching, and reporting back a clear, complete answer to whatever you were asked to \
investigate. You DO have `bash` (needed for real find/grep-style exploration across the repo) -- \
use it only for read-only exploration (find, grep, ls, cat, git log/diff/status), never to modify \
anything. A genuinely destructive command from you (or anyone) now requires operator approval \
before it runs at all -- that gate exists as a real backstop, not as license to test what you can \
get away with. You also have `describe_image` for any attached screenshot/photo -- use it instead \
of `read` or your built-in read_file for image files, since those return raw bytes or fail, not a \
description. Stay strictly within what you were actually asked to investigate: if the delegation \
prompt doesn't ask you to examine an image, don't go analyze one on your own initiative -- a single \
`describe_image` call is the only appropriate way to look at one at all, never manual pixel/byte \
inspection via bash. If the prompt already states a fact (a product name, a file path, a value), \
treat it as given and move on to the actual investigation instead of re-deriving it yourself.

""" + _FILESYSTEM_GUIDANCE + """

IMPORTANT: your final report is what returns to the coordinator -- everything else you did (every \
file you read, every command you ran) stays isolated in your own context and is NOT automatically \
visible to it. Return only the essential answer: the specific finding, file paths and line numbers \
that matter, and a concise summary. Do NOT paste raw file contents, full command output, or a blow \
by blow of your own process -- a long, unfiltered report defeats the entire reason you were \
delegated to in the first place (keeping the coordinator's own context small)."""


TEST_WRITER_SYSTEM_PROMPT = """You are a test-writing subagent for a live production codebase. \
High-consequence logic (anything that moves money, mutates external state, or touches a \
third-party API) must have REAL behavioral test coverage -- tests that actually invoke the \
function against a mocked dependency and assert on real side effects.

A cautionary example of what NOT to do: a test for a state-mutating function that only asserted \
`someFunction.toString().includes('expectedCall')` -- i.e. it checked the FUNCTION'S SOURCE CODE \
as a string, never actually called the function. It would pass even if the logic were completely \
broken (wrong lock key, called with the wrong argument, a race condition mishandled). Do not write \
this kind of test. Ever.

Before reporting a test as done, call the `run_checks` tool yourself to confirm it actually runs \
and actually passes -- and read what it's asserting one more time: would this test fail if the \
underlying logic were subtly wrong? If you're not sure, it isn't a real test yet.

For frontend/UI work, LOOK at what you built before reporting done: the `webapp-testing` skill \
(read /skills/webapp-testing/SKILL.md) shows how to render the app headlessly in your bash \
sandbox, screenshot it into the workspace, and read the screenshot with `describe_image`. A \
component that compiles is not a component that renders.

Also make sure any new test file is actually registered as an npm script and included in the \
project's aggregate `test` script in package.json -- a test that exists on disk but was never wired \
in silently never runs as part of any check.

""" + _FILESYSTEM_GUIDANCE + """

Your final report returns to the coordinator; the rest of your own work stays isolated in your own \
context. Report which file(s) you wrote/changed and a short summary of what the tests actually \
cover -- not a full reprint of the test file contents (the coordinator can read the file itself if \
it needs to) and not a turn-by-turn narration of your own process."""

COORDINATOR_SYSTEM_PROMPT_TEMPLATE = """You are working on exactly one task in the repo at /workspace \
(project: {repo}). Use `write_todos` to plan and track your own work as you go -- adapt the plan as \
you learn more, rather than treating an initial plan as fixed.

ASK BEFORE GUESSING: if the goal has a consequential ambiguity -- two \
reasonable interpretations that lead to materially different work (which \
items to move, which of two approaches the operator named, destructive vs \
additive changes) -- use the `ask_user` tool with ONE focused question and \
concrete options BEFORE starting the large work, then follow the answer as \
authoritative. Never ask about things the repo itself can answer (read the \
code instead), and never ask more than one question at a time. A wrong \
guess costs a full build-review-rework cycle; a question costs one minute.

DELEGATE TEST WORK: whenever the task calls for writing NEW tests or making non-trivial changes to \
existing test files, delegate that piece to the `test-writer` subagent via your task() tool instead \
of writing the tests yourself -- it runs a different model precisely to get an independent set of \
eyes on test quality, and a test you author yourself to validate your own implementation is exactly \
the blind spot it exists to remove. (Trivial mechanical fixes -- updating an expectation string, \
renaming an import -- are fine to do directly.)

""" + _FILESYSTEM_GUIDANCE + """

DELEGATE RESEARCH: before you can change something you usually have to find out how it works. \
The moment that costs more than a couple of looks -- you are about to open a third file, or run a \
second round of `rg` because the first did not settle it -- stop and hand the question to the \
`investigator` subagent via task(). Give it the specific question, then act on what it reports. \
Do not keep exploring inline past that point.

This is not a cost optimisation you may decline. Exploration output is bulky and almost entirely \
irrelevant once the question is answered, and it accumulates in YOUR context, which is what pushes \
this conversation into summarization -- and what summarization compacts is the earlier material: \
your plan, your findings, and the reasons behind them. The investigator spends its own context on \
the search and returns you the answer. Running one `rg` whose output you already know how to read \
is fine to do yourself; a hunt is not.

Delegate writing or hardening tests to the `test-writer` subagent, especially for anything that \
moves money or touches an external API boundary.

Call `run_checks` yourself before considering any todo done. A deterministic check failing is \
never something to argue around or reinterpret as unrelated -- if it fails, the work isn't done, \
full stop. Investigate a failure rather than asserting it's a pre-existing environment issue.

If you learn something durable and non-obvious about THIS repo specifically that would help on a \
future task (a convention, a gotcha, a concurrency primitive's real purpose, a test-wiring rule), \
write it to {memory_path} via your file-edit tool so it's there next time -- don't rediscover the \
same thing from scratch on a future task.

<org_memory path="{org_memory_path}">
Cross-project conventions that apply everywhere this agent works, not just this repo. READ-ONLY to \
you -- writes to this path are blocked at the code level, not just discouraged. Treat it as settled \
policy, not something to revise mid-task.

{org_memory_content}
</org_memory>

<project_memory path="{memory_path}">
Durable facts about THIS repo specifically, written by past runs of this same agent against this \
same project. Agent-writable -- extend it via your file-edit tool when you learn something durable \
and non-obvious (see above).

{project_memory_content}
</project_memory>

<available_skills>
Deeper, subsystem-specific reference material for this repo -- too large to keep loaded by default, \
so only names and one-line descriptions are shown here. If a listed skill sounds relevant to this \
task, read its full instructions with your read_file tool (path shown below) BEFORE making changes \
in that area -- it exists specifically because that subsystem has real, non-obvious rules that are \
easy to get wrong without it. Skills are read-only reference material, not something to edit.

{skills_summary}
</available_skills>"""


async def read_memory_or_empty(backend: StoreBackend, path: str) -> str:
    result = await backend.aread(path)
    if result.error is not None or not result.file_data:
        return "(nothing recorded yet)"
    return file_data_to_string(result.file_data)


async def build_deep_agent(
    config: Config,
    repo: str,
    budget_usd: float,
    checkpointer,
    store: BaseStore,
    starting_cost: float = 0.0,
    starting_last_failed_edit: str | None = None,
    auto_approve_commands: bool = False,
):
    """NOTE: async, unlike a typical factory -- it needs to `await` reading
    both memory files before constructing the agent. This is a deliberate
    workaround, not the deepagents-native path: `create_deep_agent`'s own
    `memory=[...]` parameter (backed by MemoryMiddleware) is broken for this
    system specifically -- confirmed empirically that MemoryMiddleware's
    `download_files`/`adownload_files` call the store synchronously
    (`store.get(...)`), and AsyncPostgresStore explicitly raises
    `InvalidStateError` on synchronous calls from within a running event loop
    ("Synchronous calls to AsyncPostgresStore detected in the main event
    loop... replace `store.get(...)` with `await store.aget(...)`"). The
    result was a silent `(No memory loaded)` in the real system prompt -- no
    exception surfaced. Reading memory ourselves via the same async-safe
    `aread()` path the rest of this module already uses sidesteps the bug
    entirely and produces the same end result (memory content in the system
    prompt) via a confirmed-working path instead of a confirmed-broken one.
    """
    repo_root = PROJECTS[repo]["sandbox"]
    # One gate, shared by the coordinator and every subagent -- investigator
    # carries the full bash tool too, so a laxer gate there would be a hole.
    interrupt_on = interrupt_on_for(auto_approve_commands)
    tracker = BudgetTracker(budget_usd=budget_usd, starting_cost=starting_cost)
    backend = build_memory_backend(repo, store)

    project_tools, last_failed_edit_ref = make_agent_tools(
        repo_root, backend=backend, initial_last_failed_edit=starting_last_failed_edit,
    )
    # Read-only SQL against this project's own application database, when it
    # has one. The codebase describes the schema; only this shows what's
    # actually IN the database -- see project_db.py for why it lives here
    # rather than inside the sandbox container.
    db_tool = make_project_db_tool(repo)
    if db_tool is not None:
        project_tools = [*project_tools, db_tool]
    tool_by_name = {t.name: t for t in project_tools}
    # No write/edit for investigator -- read/bash/describe_image are all
    # read-only against the real repo. describe_image is required, not
    # optional: without it, an investigator asked to look at an uploaded
    # image has no way to actually see one -- `read` returns raw bytes as
    # text (not a description), and the built-in read_file tool can't reach
    # the real repo filesystem at all (see _FILESYSTEM_GUIDANCE).
    read_only_tools = [tool_by_name["read"], tool_by_name["bash"], tool_by_name["describe_image"]]
    if db_tool is not None:
        read_only_tools.append(db_tool)
    run_checks_tool = _make_run_checks_tool(repo_root, repo)

    # Pinned per-role models: one fixed model per role rather than a
    # smart-router pool/classifier, so cost and accuracy are directly
    # attributable per model. The aliases live in the LLM router's own
    # config: to swap a role's model, edit the router config, not this file.
    # Coordinator gets two models -- planner for the first turn of a thread
    # (the one that writes the todo plan), coder for every turn after -- via
    # PlanCodeModelMiddleware below.
    coordinator_model = llm_for_role(config, "agent-coder")
    planner_model = llm_for_role(config, "agent-planner")
    investigator_model = llm_for_role(config, "agent-investigator")
    test_writer_model = llm_for_role(config, "agent-test-writer")

    # Stripped keys (route_local_path), not the full agent-visible paths --
    # this read must land on the same key the agent's own file tools write
    # to (via the composite's route stripping), or agent-written memory
    # updates are invisible to every future task's prompt.
    project_memory_backend = StoreBackend(namespace=project_namespace(repo), store=store)
    org_memory_backend = StoreBackend(namespace=org_namespace, store=store)
    project_memory_content = await read_memory_or_empty(project_memory_backend, route_local_path("/memories/", MEMORY_PATH))
    org_memory_content = await read_memory_or_empty(org_memory_backend, route_local_path("/org-memory/", ORG_MEMORY_PATH))
    skills_summary = await load_skills_summary(repo, store)

    # skills=[SKILLS_ROUTE] on each subagent: per deepagents' own docs, only
    # the general-purpose subagent automatically inherits main-agent skills
    # -- custom subagents require an explicit skills parameter. Without this,
    # investigator (the subagent doing exactly the deep multi-file
    # exploration a domain skill matters most for) never saw the skills
    # manifest at all. Uses the native deepagents SkillsMiddleware here
    # (unlike the coordinator's own manual-injection workaround below) --
    # confirmed to correctly await backend.als()/adownload_files() and load
    # real data against our AsyncPostgresStore-backed CompositeBackend with
    # no error. MemoryMiddleware (the `memory=` param) is still broken the
    # same way it always was, so this fix is scoped to skills specifically,
    # not a signal to also switch the coordinator's own proven-working
    # memory/skills prompt injection over to native params.
    investigator = {
        "name": "investigator",
        "description": (
            "Delegate read-only research/exploration here: mapping out how something works across "
            "multiple files, finding every call site of something, or answering a question that "
            "needs digging before any change can be made. Cannot write or edit files."
        ),
        "system_prompt": INVESTIGATOR_SYSTEM_PROMPT,
        "tools": read_only_tools,
        "model": investigator_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            HiddenToolsMiddleware("glob", "grep", "execute"),
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        # investigator has the full `bash` tool despite its own "read-only"
        # framing (needed for real find/grep-across-the-tree exploration --
        # see INTERRUPT_ON's own comment) -- this is what actually gates a
        # destructive command from it, since nothing else does at the code
        # level.
        "interrupt_on": interrupt_on,
    }

    test_writer = {
        "name": "test-writer",
        "description": (
            "Delegate here to write or harden tests, especially for anything that moves money, "
            "mutates external state, or touches a third-party API. Must produce real behavioral "
            "coverage, never source-inspection-only tests."
        ),
        "system_prompt": TEST_WRITER_SYSTEM_PROMPT,
        "tools": [*project_tools, run_checks_tool],
        "model": test_writer_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            HiddenToolsMiddleware("glob", "grep", "execute"),
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        "interrupt_on": interrupt_on,
    }

    # Explicit general-purpose subagent: unless a spec with this exact name
    # exists, create_deep_agent auto-adds its own general-purpose subagent
    # carrying the coordinator's full tools and model but not its custom
    # middleware (deepagents' graph.py deliberately inherits only middleware
    # that overrides a default GP slot, without carrying over middleware
    # that's specific to the main agent). That means any work the
    # coordinator delegated to general-purpose would run with no
    # BudgetGuardMiddleware (its LLM spend invisible to the hard dollar
    # ceiling -- one of this system's two non-negotiable code-enforced
    # guards) and no call-limit backstops. Defining it inline with the same
    # name suppresses the auto-add (graph.py checks by name) while the
    # subagent factory still attaches its default slots (filesystem/
    # summarization/skills), same as investigator/test-writer get -- so
    # this keeps the capability and closes the enforcement hole.
    general_purpose = {
        **GENERAL_PURPOSE_SUBAGENT,  # canonical name/description/system_prompt from the lib
        "system_prompt": GENERAL_PURPOSE_SUBAGENT["system_prompt"] + "\n\n" + _FILESYSTEM_GUIDANCE,
        "tools": [*project_tools, run_checks_tool],
        "model": coordinator_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            HiddenToolsMiddleware("glob", "grep", "execute"),
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        "interrupt_on": interrupt_on,
    }

    agent = create_deep_agent(
        model=coordinator_model,
        tools=[*project_tools, run_checks_tool, _make_ask_user_tool()],
        system_prompt=COORDINATOR_SYSTEM_PROMPT_TEMPLATE.format(
            # No repo_root here -- the prompt says "/workspace" literally
            # (see _FILESYSTEM_GUIDANCE): the real host repo_root path is
            # meaningless to the model now that `bash` runs sandboxed --
            # that path doesn't exist inside the container at all.
            repo=repo,
            memory_path=MEMORY_PATH,
            org_memory_path=ORG_MEMORY_PATH,
            project_memory_content=project_memory_content,
            org_memory_content=org_memory_content,
            skills_summary=skills_summary,
        ),
        middleware=[
            HiddenToolsMiddleware("glob", "grep", "execute"),  # see subagent specs' comment
            BudgetGuardMiddleware(tracker),
            # Planner on the thread's first turn, coder after -- see model_pin.py.
            PlanCodeModelMiddleware(planner_model, coordinator_model),
            SummarizationMiddleware(
                # Meter callback, not middleware: this model is ainvoke()d
                # directly by SummarizationMiddleware, a path no agent
                # middleware wraps -- see BudgetMeterCallback.
                model=llm_for_role(config, "agent-summarizer", callbacks=[BudgetMeterCallback(tracker)]),
                trigger=SUMMARIZATION_TRIGGER,
                keep=SUMMARIZATION_KEEP,
                trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS,
            ),
            # Not included by default for a custom model like ours --
            # TodoListMiddleware is only auto-added for specific built-in
            # harness profiles, not universally, so it's added explicitly
            # here.
            TodoListMiddleware(),
            # Defense-in-depth backstop against a runaway loop -- see this
            # module's own comment on MODEL_CALL_RUN_LIMIT/TOOL_CALL_RUN_LIMIT
            # for why these are generous limits, not a normal-operation cap.
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        subagents=[general_purpose, investigator, test_writer],
        interrupt_on=interrupt_on,
        # No `memory=[...]` here -- see build_deep_agent's own docstring for
        # why: MemoryMiddleware's automatic loading is broken against
        # AsyncPostgresStore specifically. Content is already embedded
        # directly in system_prompt above via the confirmed-working async
        # read path instead.
        backend=backend,
        # Code-level enforcement, not a prompt instruction: org-memory is
        # populated by application code only (seed_org_memory) -- one
        # task's agent must never be able to alter what every other
        # project's agent believes is settled policy. Skills get the same
        # treatment -- curated reference material (seed_skill), not
        # something to drift via ad hoc mid-task edits the way /memories/
        # is deliberately allowed to.
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/org-memory/*"], mode="deny"),
            FilesystemPermission(operations=["write"], paths=["/skills/*"], mode="deny"),
        ],
        checkpointer=checkpointer,
        store=store,
    )

    return agent, tracker, last_failed_edit_ref
