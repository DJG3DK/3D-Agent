"""Planning chat -- a conversational, research/design-consulting agent
distinct from the build system's own coordinator (agent/deep_agent.py). No
outer plan/execute/verify graph here: this is a single deepagents graph
invoked directly per chat turn, its own conversation persisted via the same
Postgres checkpointer the build tasks use, under a distinct thread_id
namespace (f"planning:{session_id}") that never collides with a task's own
thread_id (a plain string).

Deliberately no write/edit/bash against the primary repo -- no
INTERRUPT_ON-style approval gate is needed the way investigator/test-writer
require for their bash access, since nothing here can mutate a real repo's
source. It DOES get real, persistent memory access though (see below): a
planning conversation that can't remember anything between sessions isn't
much of a planning assistant, and this system already has a proven, working
memory mechanism (build_deep_agent's own /memories//org-memory setup) --
reusing it here rather than inventing a separate planning-only memory store
keeps one project's "what we know" in one place, shared by both planning
sessions and real build tasks.

Same shared memory backend as build_deep_agent (build_memory_backend), keyed
to this session's own primary repo -- both `/memories/AGENTS.md` (real,
project-scoped, agent-writable) and `/org-memory/AGENTS.md` (real,
cross-project, read-only -- same deny-write permission build tasks get) are
embedded into the system prompt AND backed by a real StoreBackend, so
deepagents' own native write_file tool can actually persist an update to
project memory (unlike the plain StateBackend fallback a planning agent with
no backend= would get, where write_file goes nowhere real). This is the
"remember stuff across sessions" mechanism: whatever a planning conversation
writes to /memories/ is visible to every future planning session AND every
future build task for that project, and vice versa.

Read access is deliberately cross-project though (list_project_dir/
read_project_file, agent/tools/planning_tools.py) -- planning is explicitly
allowed to look at a DIFFERENT one of the operator's projects for comparison
or inspiration, unlike a build task's single-repo scope. Memory stays scoped
to the session's own primary repo either way; there's no cross-project
memory write from here.

Two models, not one (2026-08-23): agent-planning-chat (Gemini 3.7 Flash,
cheap) handles the default case -- web search, research, simple design/UX
planning (colors, layout, theme direction) -- and agent-planning-chat-hard
(Qwen3.8 Max, the same model agent-investigator/agent-consolidator already
use) is reserved for a turn that actually reads as bug-fixing/debugging or a
genuinely hard problem. classify_planning_difficulty picks between them
FRESH on every turn (a deterministic keyword floor first, then routing
through the SAME classify_task classifier this system already built for
task categorization -- its own "bug-fix" category is the HARD signal, no
second parallel classifier needed) -- no session-level pinning, same
reasoning as the shared router's own smart-router: a conversation can drift
from "what color should this be" into "why is this crashing" mid-session,
and the model should follow that, not stay stuck on whichever tier the
first message happened to classify as. Everything else about the agent --
tools, memory backend, permissions, middleware -- is identical between the
two; only `model=` changes, so Qwen gets exactly the same access Gemini
does, never a reduced set.
"""

import logging
import math
import time

from langchain.agents.middleware import ModelCallLimitMiddleware, SummarizationMiddleware, ToolCallLimitMiddleware
from langgraph.store.base import BaseStore

from deepagents import FilesystemPermission, create_deep_agent

from agent.classify import classify_task
from agent.message_text import content_text
from agent.config import Config, PROJECTS
from agent.deep_agent import (
    MEMORY_PATH,
    MODEL_CALL_RUN_LIMIT,
    ORG_MEMORY_PATH,
    SUMMARIZATION_KEEP,
    SUMMARIZATION_TRIGGER,
    SUMMARIZATION_TRIM_TOKENS,
    TOOL_CALL_RUN_LIMIT,
    build_memory_backend,
    llm_for_role,
    load_skills_summary,
    org_namespace,
    project_namespace,
    read_memory_or_empty,
    route_local_path,
)
from agent.middleware.hidden_tools import HiddenToolsMiddleware
from agent.middleware.budget_guard import BudgetMeterCallback, BudgetGuardMiddleware, BudgetTracker
from agent.model_config import resolve_alias
from agent.tools.agent_tools import make_agent_tools
from agent.tools.planning_tools import make_planning_tools
from deepagents.backends import StoreBackend

logger = logging.getLogger("3d-agent")

# Deterministic floor, checked before the classifier call below -- mirrors
# llm-router/config.yaml's own smart-router keyword_tier_rules philosophy
# (its exact REASONING-tier floor is "super hard", "extremely difficult",
# "think as hard as you can", "hardest part", "spare no effort" -- echoed
# here since classify_task's own bug-fix/feature/ui-styling/performance/
# investigation/other taxonomy has no "this is a hard problem" category of
# its own to catch that phrasing). An obvious case shouldn't depend on a
# classifier call (cost, latency, one more thing that can fail) when a plain
# keyword match already settles it. Kept short and specific, not a broad net
# -- a false HARD just costs a pricier turn, but a broad net would defeat
# the point of having a cheap default tier at all.
# Per-turn spend ceiling for a planning conversation -- generous enough for
# a genuinely hard investigation (the deepest useful sessions have landed
# $1-2 with caching), tight enough that a runaway marathon is interrupted
# while it is a surprise, not a bill.
PLANNING_TURN_BUDGET_USD = 4.0

_HARD_KEYWORDS = (
    "super hard", "extremely difficult", "think as hard as you can",
    "hardest part", "spare no effort", "really hard", "difficult problem",
    # The operator's actual phrasing when they want the better model
    # (2026-08-26: "this is a complex request" routed EASY because nothing
    # here matched it). Deliberately multi-word or unambiguous forms only --
    # a bare "hard" would match "hard-coded", a bare "complex" is safe but
    # "complicated"/"tricky" cover the natural synonyms.
    "complex", "complicated", "tricky", "this is hard", "very hard",
)


async def classify_planning_difficulty(text: str, config: Config) -> str:
    """Returns "EASY" or "HARD" -- which of the two planning-chat models
    (see this module's docstring) should handle this turn.

    Routed through the SAME classifier this system already built for task
    categorization (agent/classify.py's classify_task, the pinned
    agent-classifier alias) rather than a second, parallel one:
    classify_task's own "bug-fix" category is exactly the HARD signal this
    needs, and reusing it means one classifier, one cost/latency profile,
    one thing to trust -- not two doing overlapping jobs. classify_task is
    already best-effort (falls back to "other" on any failure), so this
    inherits that same fail-toward-cheap behavior for free rather than
    needing its own separate fallback handling.
    """
    lowered = text.lower()
    if any(keyword in lowered for keyword in _HARD_KEYWORDS):
        return "HARD"
    classification = await classify_task(text, config)
    return "HARD" if classification.category == "bug-fix" else "EASY"

PLANNING_SYSTEM_PROMPT = """You are a planning and research assistant. Your job is a conversation, not a \
build: help the user think through a project -- researching options, discussing design/UI/UX direction, \
asking clarifying questions -- until there's a clear, complete plan. You never write or edit code yourself; \
a separate build system does that afterward, from the plan document you produce here.

You are consulting on: the "{repo}" project. The operator's other configured projects are: {other_repos} -- \
you can read any of them (not just "{repo}") via list_project_dir/read_project_file when it's useful to \
compare approaches or borrow a pattern.

What this project's own memory already holds (accumulated from past planning sessions and real build \
tasks -- treat it as established fact about this project, not something to re-derive):
--- /memories/AGENTS.md ({repo}) ---
{project_memory_content}

Cross-project notes (apply to every project, not just this one):
--- /org-memory/AGENTS.md ---
{org_memory_content}

This project also has REGISTERED SKILLS -- durable curated reference docs your built-in read_file tool CAN \
read (they live in your own file space, not the repo):
{skills_summary}

START every code investigation with the CODEBASE MAP: read /skills/codebase-map/SKILL.md (with built-in \
read_file) BEFORE walking the repo with list_project_dir. It is the maintained map of "{repo}" -- where \
things live, entry points, conventions -- rebuilt on a schedule from the real tree. One read replaces a \
dozen exploratory directory listings and wrong-path guesses. Trust it for orientation; verify specifics \
with read_project_file (the map can lag a very recent rename).

Tools available to you:
- read_project_file(repo, path) / list_project_dir(repo, path): read/list files in any configured \
project's real repo (read-only). BOTH arguments every time -- `repo` (default to "{repo}" unless you're \
deliberately looking at another one) AND `path`, repo-relative. read_project_file's `path` is required and \
names one file ("src/App.tsx"); calling it with only `repo` is a wasted round trip that just comes back \
"path: Field required". list_project_dir's `path` defaults to the repo root, so pass "." when you mean it. \
Paths run through the repo's real working tree, so nothing under .git/ is readable -- ask for a file, not \
for git metadata. A big file comes back TRUNCATED: asking for it again returns the identical text, so page \
through it with read_project_file(repo, path, offset=<1-based line>, limit=<lines>) in big windows \
(600-800 lines), never by re-requesting the whole file and never in 50-line slices.
- write_file: your own write access is limited to /memories/AGENTS.md (this project's memory) -- use it to \
record a durable fact worth remembering for next time (a decision made, a constraint discovered, a \
direction the user confirmed). Don't use it for anything else; everything else you "write" doesn't \
actually go anywhere real. You cannot write to /org-memory/ at all.
- describe_image: look at an image the user attached.
- web_search: search the web for research, documentation, or design inspiration.
- browse_page: load a real webpage and read it -- pass screenshot=True to also get a description of what \
it actually looks like (layout, colors, typography), which is exactly what you need when the user wants to \
reference or compare a design/competitor site.
You have NO way to delegate: there are no subagents here, and investigation you want done is \
investigation you do yourself, with the tools above. Read the files, page through the big ones, and reason \
about what you found -- that is the job, not a detour from it. If a question is too big to answer fully in \
one turn, say what you found, say what is still open, and save the plan you can justify now; a partial \
plan the operator can act on beats a perfect one that never gets written.
- save_plan: save the current draft plan document (Markdown). Call this once the plan is genuinely ready, \
and again any time it meaningfully changes -- never wait to be asked. The user can hit "Build Now" the \
moment a plan exists, so don't leave a stale or half-finished draft saved if the conversation has moved on. \
CRITICAL: your chat reply is NOT the plan. A plan, report, or spec that exists only as message text cannot \
be built from and is lost the moment the conversation moves on -- if your reply contains the plan document, \
you MUST call save_plan with that same content in the SAME turn, then summarize briefly in chat. Ending a \
turn with a plan in prose and no save_plan call is a failure, not a style choice.

IMPORTANT -- your built-in `ls`/`read_file`/`write_file`/`edit_file` tools see YOUR OWN file space \
(/memories/, /org-memory/, /skills/ -- including the codebase map above), NEVER the actual repo, no matter \
what path you give them. A "file not found" from THOSE tools on a repo-looking path does not mean the file \
doesn't exist in the real project -- it means you used the wrong tool: switch to read_project_file/\
list_project_dir, the ONLY tools (plus describe_image, for attached images) that ever reach the real repo. \
There is NO repo grep here at all -- to find something in the repo, read the codebase map first, then read \
the specific files it points you to; repeating a failed lookup harder is never the fix. If \
read_project_file/list_project_dir themselves come up empty, that's when it's actually real -- say so \
plainly instead of guessing, and don't let a research dead end turn into losing track of what the user \
actually asked for.

Whenever you mention a specific color (a palette, an accent, anything from a screenshot's visual \
description), always give a real value -- a hex code (#3fb950) or rgb(...) -- alongside any descriptive \
name, never a name or vague description alone ("a nice teal" is not enough, "#008080 (teal)" is). The \
chat interface renders an actual color swatch next to any hex/rgb value you write, so this is how the \
user actually sees the color, not just reads about it -- a color with no real value attached is invisible \
to them.

The plan document you save must be self-contained -- a build agent with no memory of this conversation \
has to be able to act on it directly. Include: the concrete goal, key requirements/decisions made here, \
and any design/UX direction (with specifics: colors, layout choices, references found) -- not just a \
summary of the chat. Prefer asking a clarifying question over guessing at an ambiguous requirement, but \
don't stall on genuinely minor details a builder could reasonably decide on its own."""


def planning_thread_config(session_id: str, repo: str) -> dict:
    return {
        "configurable": {"thread_id": f"planning:{session_id}"},
        "metadata": {"planning_session_id": session_id, "repo": repo},
        "tags": [repo, "planning"],
    }


async def build_planning_agent(
    config: Config,
    repo: str,
    checkpointer,
    store: BaseStore,
    starting_cost: float = 0.0,
    difficulty: str = "EASY",
    existing_plan: str | None = None,
    allowed_repos: list[str] | None = None,
):
    """Returns (agent, plan_ref, tracker).

    `difficulty` ("EASY" or "HARD", from classify_planning_difficulty)
    picks which of the two planning-chat models actually answers this turn
    -- see this module's docstring. Defaults to "EASY" for callers that
    build the agent without driving a real turn (e.g. get_planning_session's
    read-only state fetch, which never invokes the model at all, so which
    one gets wired in there is inert). Every other argument to
    create_deep_agent below -- tools, memory backend, permissions,
    middleware -- is identical regardless of `difficulty`; only `model=`
    changes, so the harder model never gets a reduced tool/memory set.

    `plan_ref` is a mutable {"markdown": str | None} dict that
    make_planning_tools' save_plan tool writes into; the caller reads it
    back after each turn to persist the current draft into the session's
    Store meta (see run_planning_turn).

    `tracker` is a BudgetTracker with an effectively infinite ceiling --
    unlike a build task, planning chat has no $ cap (the operator explicitly
    doesn't want one here), but the cost is still worth showing, and
    BudgetGuardMiddleware/BudgetTracker already compute it correctly from
    real token usage against the router's own pricing -- reusing that
    exactly rather than a separate ad hoc cost calculation.
    """
    repo_root = PROJECTS[repo]["sandbox"]
    project_tools, _ = make_agent_tools(repo_root)
    tool_by_name = {t.name: t for t in project_tools}
    planning_tools, plan_ref = make_planning_tools(existing_plan, allowed_repos)

    project_memory_backend = StoreBackend(namespace=project_namespace(repo), store=store)
    org_memory_backend = StoreBackend(namespace=org_namespace, store=store)
    project_memory_content = await read_memory_or_empty(project_memory_backend, route_local_path("/memories/", MEMORY_PATH))
    org_memory_content = await read_memory_or_empty(org_memory_backend, route_local_path("/org-memory/", ORG_MEMORY_PATH))
    # audit H-2: render only repos this session's user may actually read, not
    # every configured project -- the prompt used to advertise "you can read any
    # of them" across all of PROJECTS regardless of the caller's allow-list.
    _visible = [r for r in PROJECTS if r != repo and (allowed_repos is None or r in allowed_repos)]
    other_repos = ", ".join(_visible) or "(none)"

    # A real per-turn dollar ceiling (2026-08-28). This was math.inf -- the
    # one agent with NO budget was the one the operator watched burn $7 on a
    # single 157-call, 10.6M-input-token turn. Tasks have hard budgets
    # enforced per model call; planning now does too, scoped per TURN (the
    # ceiling rides on top of whatever the session has already spent, so a
    # long session isn't strangled by its own history -- each turn gets the
    # same allowance). Healthy turns run $0.10-$1.50; the runaways are the
    # investigate-everything marathons this exists to interrupt. On breach,
    # BudgetGuardMiddleware raises out of the turn; the teardown paths bank
    # the draft plan, the spend, and fire the planning_error Telegram alert,
    # so nothing is lost -- the operator decides whether another turn's
    # allowance is worth it with a one-line "continue".
    tracker = BudgetTracker(
        budget_usd=starting_cost + PLANNING_TURN_BUDGET_USD,
        starting_cost=starting_cost,
    )
    skills_summary = await load_skills_summary(repo, store)

    planning_model_role = "agent-planning-chat-hard" if difficulty == "HARD" else "agent-planning-chat"
    agent = create_deep_agent(
        model=llm_for_role(config, planning_model_role, reasoning_effort="high", timeout=450),
        tools=[tool_by_name["describe_image"], *planning_tools],
        system_prompt=PLANNING_SYSTEM_PROMPT.format(
            repo=repo,
            other_repos=other_repos,
            project_memory_content=project_memory_content,
            org_memory_content=org_memory_content,
            skills_summary=skills_summary,
        ),
        middleware=[
            # create_deep_agent adds a general-purpose subagent (and therefore
            # a `task` tool) unconditionally -- `subagents=None` does NOT mean
            # none, see agent/middleware/hidden_tools.py. This module is built
            # as one flat conversation and its system prompt never mentions
            # `task`, but the model found it in the schema anyway and spent
            # whole turns inside nested agent loops: 115 model calls and 4.7M
            # input tokens against the 1800s ceiling on 2026-08-27, timing out
            # with only ~22 tool calls in its own transcript. That subagent
            # also carried no BudgetGuardMiddleware (it has to be attached per
            # spec by hand), so the same turn reported $0.44 against $3.98 of
            # real router spend. Withholding the tool closes both.
            # grep/glob joined the hidden list 2026-08-27: they search the
            # SCRATCH space, not the repo, and despite the prompt's warning a
            # live session burned 8 consecutive identical grep('runBacktest',
            # 'src/core/backtester...') calls -- "No matches found" every time
            # -- against a repo path the tool cannot see. The repo has no grep
            # tool here at all now; the codebase map (see prompt) is the
            # find-things mechanism. execute/delete go too: execute has no
            # sandbox behind it in this agent, delete has nothing it should
            # ever delete.
            HiddenToolsMiddleware("task", "grep", "glob", "execute", "delete"),
            BudgetGuardMiddleware(tracker),
            SummarizationMiddleware(
                # Metered via callback -- SummarizationMiddleware ainvoke()s
                # this model directly, outside any agent middleware (see
                # BudgetMeterCallback in budget_guard.py).
                model=llm_for_role(config, "agent-summarizer", callbacks=[BudgetMeterCallback(tracker)]),
                trigger=SUMMARIZATION_TRIGGER,
                keep=SUMMARIZATION_KEEP,
                # Not optional -- the library default (4000) is a REAL, live
                # bug here, confirmed 2026-08-23: a planning turn opens with
                # one long HumanMessage (the operator's own detailed
                # problem/ask) followed by a long tool-heavy stretch
                # (repo reads/greps), the exact shape deep_agent.py's own
                # SUMMARIZATION_TRIM_TOKENS comment already documents as
                # producing an empty trim once that HumanMessage sits
                # further back than any finite budget reaches. The
                # summarizer call then fails outright and the middleware
                # silently substitutes "Previous conversation was too long
                # to summarize" for the ENTIRE history -- destroying the
                # original ask with no error surfaced anywhere, which is
                # exactly why a planning session went silent and asked the
                # user to restate a problem it had just been given in
                # detail. SUMMARIZATION_TRIGGER already bounds the untrimmed
                # batch at fire time, so trimming is redundant here too --
                # disable it the same way the coordinator already does.
                trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS,
            ),
            ModelCallLimitMiddleware(run_limit=MODEL_CALL_RUN_LIMIT, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=TOOL_CALL_RUN_LIMIT, exit_behavior="error"),
        ],
        # Real memory backend (not the plain StateBackend fallback) -- gives
        # deepagents' own native write_file/edit_file tools somewhere real to
        # write, scoped to this project's memory. Everything else (any other
        # path a model tries) still falls back to StateBackend's ephemeral
        # scratch space, same as before -- this only adds the two memory
        # routes, it doesn't grant real filesystem access anywhere else.
        backend=build_memory_backend(repo, store),
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/org-memory/*"], mode="deny"),
        ],
        checkpointer=checkpointer,
        store=store,
    )
    return agent, plan_ref, tracker


def _translate_message(msg) -> dict | None:
    """Mirrors agent/nodes/work.py's _translate_message, simplified: no
    node_label (planning chat has no subagents to distinguish)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    if isinstance(msg, HumanMessage):
        # The operator's own turns. Previously dropped entirely (this
        # translator only knew AIMessage/ToolMessage), so a refreshed page
        # showed the agent answering nobody. Summarization's synthetic
        # "Here is a summary..." HumanMessage is internal plumbing, not an
        # operator turn -- skipped rather than rendered as a giant fake
        # user bubble.
        text = content_text(msg.content).strip()
        if not text or text.startswith("Here is a summary of the conversation"):
            return None
        return {
            "kind": "user",
            "summary": text[:200],
            "detail": text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    if isinstance(msg, AIMessage):
        if msg.tool_calls:
            calls = ", ".join(f"{tc['name']}({str(tc.get('args'))[:80]})" for tc in msg.tool_calls)
            summary = f"calling: {calls}"
        else:
            text = content_text(msg.content).strip()
            if not text:
                return None
            summary = text[:200]
        response_metadata = getattr(msg, "response_metadata", None) or {}
        alias = response_metadata.get("model_name") or response_metadata.get("model")
        # content_text, never str(content): Kimi block-list content rendered
        # as raw dict repr here, which the operator read as failed/no-output
        # calls (see agent/message_text.py). A tool-call-only turn falls back
        # to the call summary instead of showing block debris.
        return {
            "kind": "agent",
            "summary": summary,
            "detail": (content_text(msg.content).strip() or summary)[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": resolve_alias(alias),
        }
    if isinstance(msg, ToolMessage):
        tool_text = content_text(msg.content)
        return {
            "kind": "tool-result",
            "summary": f"tool result: {tool_text[:200]}",
            "detail": tool_text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    return None


async def run_planning_turn(agent, plan_ref: dict, thread_config: dict, text: str | None, publish, tracker=None) -> str | None:
    """Runs one turn (text=None resumes/continues the thread with no new
    input, e.g. after a process restart) and publishes each new message via
    `publish(event)` as it happens, mirroring work.py's live-streaming
    approach at a smaller scale (one flat conversation, no subagents/todos to
    multiplex). Returns the current plan_ref["markdown"] after the turn
    (None if never saved).
    """
    from langchain_core.messages import HumanMessage

    graph_input = {"messages": [HumanMessage(content=text)]} if text else None
    # Each turn is its own astream_events() call (unlike work.py's single
    # continuous run for a whole task), and `run.values` always reports the
    # FULL accumulated message list, not a delta -- without seeding
    # seen_count from what's already in the checkpoint, every turn would
    # re-publish the entire conversation history from message 0, not just
    # what's new this turn.
    existing = await agent.aget_state(thread_config)
    seen_count = len(existing.values.get("messages", [])) if existing and existing.values else 0
    _last_cost_emitted = tracker.total_cost if tracker is not None else 0.0
    async with await agent.astream_events(graph_input, config=thread_config, version="v3") as run:
        async for values in run.values:
            if not isinstance(values, dict):
                continue
            # Live cost, same contract as a build task's stream (operator
            # report 2026-08-28: "doing a plan live now and it still shows 0
            # cost" -- planning only reported cost at turn_complete). Any
            # real move emits; the superstep cadence bounds volume.
            if tracker is not None and tracker.total_cost - _last_cost_emitted >= 0.0001:
                _last_cost_emitted = tracker.total_cost
                publish({"type": "cost", "cost_usd": tracker.total_cost})
            messages = values.get("messages") or []
            if len(messages) > seen_count:
                for msg in messages[seen_count:]:
                    translated = _translate_message(msg)
                    # kind=="user" entries are NOT published live: the client
                    # renders its own copy the moment the operator hits send,
                    # and the turn handler seeds the live-log buffer with it
                    # -- streaming the translated HumanMessage too painted a
                    # SECOND user bubble (with the attachments note appended)
                    # right under the first (reported live 2026-08-28). The
                    # translation exists for checkpoint HYDRATION, where no
                    # client-side copy exists.
                    if translated and translated.get("kind") != "user":
                        publish(translated)
                seen_count = len(messages)
    if plan_ref.get("markdown") is None:
        # Safety net for a model that writes the plan as CHAT TEXT and ends
        # the turn without ever calling save_plan -- which a real session did
        # on 2026-08-27: a $6.12 turn compiled a full 8k-char audit report
        # into its final message, called no tool, and left the plan panel
        # empty (the operator read the older plan visible on ANOTHER session
        # as a cross-save). The prompt now forbids it outright, but a prompt
        # is a request, not a guarantee. Heuristic is deliberately narrow --
        # a long final message with real markdown-heading structure -- so an
        # ordinary conversational answer never lands in the plan panel; and
        # this only fills an ABSENT plan (plan_ref None means no save_plan
        # this turn; the caller's preserve-not-clobber rule still governs
        # what gets stored).
        try:
            state = await agent.aget_state(thread_config)
            messages = (state.values or {}).get("messages", []) if state else []
            from langchain_core.messages import AIMessage as _AIMessage
            last = messages[-1] if messages else None
            if isinstance(last, _AIMessage) and not getattr(last, "tool_calls", None):
                text = content_text(last.content).strip()
                if len(text) >= 1500 and text.count("\n#") >= 3:
                    logger.warning("planning turn ended with an unsaved plan-shaped reply "
                                   "(%d chars) -- adopting it as the draft plan", len(text))
                    plan_ref["markdown"] = text
        except Exception:  # noqa: BLE001 -- the net must never break a healthy turn
            logger.exception("unsaved-plan recovery failed; returning without a plan")
    return plan_ref.get("markdown")
