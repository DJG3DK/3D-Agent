"""Tools for the planning chat agent (agent/planning_chat.py) -- a
research/consulting role, not a code-editing one. No write/edit/bash here
deliberately: the planning chat never mutates the target repo itself, it
only reads it for context and drafts a plan document that later gets handed
to a real build task (which has its own full write/edit/bash toolset). That
also means there's no INTERRUPT_ON-style approval gate needed here, unlike
investigator/test-writer -- nothing in this tool list can touch the
filesystem destructively.

Playwright (not a plain httpx GET) is the actual point of `browse_page`: a
raw HTTP fetch can't render JS-heavy pages or take a real screenshot, and
"how does this page actually look" (colors, layout, spacing) is exactly what
a design/UX planning conversation needs that a text-only fetch can't give.
Confirmed working headless on this VPS (chromium already cached under
~/.cache/ms-playwright from another project's Playwright install) with the
same --no-sandbox launch args that project already uses in production.

web_search scrapes Bing's HTML results, not an API -- there's no search API
key configured anywhere in this deployment (see .env), and DuckDuckGo's own
HTML/lite endpoints return a hard 403 from this VPS's IP (confirmed, not a
scraping bug on this end -- likely datacenter-IP blocking on DDG's side).
Bing has no such block and returns real, parseable results. This is a
scraping dependency, not a stable API contract -- if Bing changes its markup
this will need updating. Swapping in a paid search API (Tavily/Brave/Bing
API) later is a drop-in replacement for just this one function.
"""

import base64
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

from langchain_core.tools import tool

from agent.tools.url_guard import UnsafeUrlError, assert_public_url, make_route_guard

from agent.config import PROJECTS
from agent.tools.files import BinaryFileError, PathEscapeError, read_file
from agent.tools.files import _resolve
from agent.tools.tool_errors import tool_errors_to_text
from agent.tools.vision import describe_image_bytes

# audit M-8: --ignore-certificate-errors removed -- this browser renders
# attacker-influenced pages (browse_page on a model-chosen URL, live Bing
# results) whose text enters the model context, so TLS validation must stay
# on; a network-position attacker could otherwise substitute page content.
# --no-sandbox is retained (the container/user story is tracked in M-10) but
# is no longer paired with disabled cert checks.
_LAUNCH_ARGS = ["--no-sandbox", "--disable-setuid-sandbox"]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 20_000
_PAGE_TEXT_CAP = 6_000
_SEARCH_RESULT_CAP = 10

# How much of a file read_project_file will put in the conversation at once.
# 40_000 is read_file's own default, kept deliberately: this is the size a
# plain read has always returned, so nothing that used to fit changes shape.
# What changes is what happens ABOVE it -- see the tool.
_READ_INLINE_CAP_CHARS = 40_000
# The paging path has to be able to reach past the inline cap, so the actual
# read is bounded far higher (same ceiling agent_tools.read uses).
_READ_HARD_CAP_CHARS = 2_000_000

# A single file may be read this many times in ONE planning turn before the
# tool starts pushing back, and this many before it refuses outright.
#
# Session 0616917 (2026-08-31) read src/core/bot.js 129 times and
# config/pairs.json 103 times in one turn, burning its whole $8 ceiling
# without ever calling save_plan. The underlying cause was summarization
# evicting what had just been read (see PLANNING_SUMMARIZATION_TRIGGER), and
# that is fixed separately -- but a context window is a soft bound and a loop
# that big should not be reachable at all. This is the hard one.
#
# 14 is deliberately generous: paging a 4,000-line file in 500-line windows
# takes 8 reads, so a full legitimate page-through plus slack still fits. 129
# does not.
_READ_WARN_AT = 6
_READ_CAP = 14


@asynccontextmanager
async def _browser_page(viewport=None):
    """One headless Chromium page per call -- tool calls here are
    infrequent enough (a handful per planning turn, not a hot loop) that
    launching fresh each time is simpler and safer than managing a shared
    long-lived browser instance across concurrent planning sessions."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            page = await browser.new_page(user_agent=_USER_AGENT, viewport=viewport or {"width": 1280, "height": 800})
            yield page
        finally:
            await browser.close()


def _decode_bing_redirect(href: str) -> str:
    """Bing wraps every result link in a bing.com/ck/a tracking redirect --
    the real target URL is base64 (urlsafe, unpadded) in the `u` query
    param, prefixed with a literal "a1". Falls back to the raw href
    (still a working link, just via Bing's redirect) if the shape ever
    changes -- never worth failing the whole search over one bad link."""
    try:
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            b64 = u[2:].replace("-", "+").replace("_", "/")
            b64 += "=" * (-len(b64) % 4)
            # validate=True: plain b64decode silently ignores characters
            # outside the base64 alphabet by default (not an error) --
            # garbage input would otherwise decode to a garbage-but-
            # "successful" string instead of raising, defeating the
            # except-fallback below. (urlsafe_b64decode itself has no
            # validate param -- -/_ are translated to +// manually instead.)
            # Strict utf-8 decoding for the same reason: a genuine decode
            # failure should fall back to href, not silently return mangled
            # text dressed up as a URL.
            return base64.b64decode(b64, validate=True).decode("utf-8")
    except Exception:
        pass
    return href


async def _run_web_search(query: str, num_results: int) -> str:
    from urllib.parse import quote_plus

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    async with _browser_page() as page:
        try:
            await page.goto(
                # audit M-29: encode the query. Unencoded, a `&` split it into a
                # second URL param and a `#` dropped everything after it, so Bing
                # silently answered a truncated query ("react & vue" -> "react").
                f"https://www.bing.com/search?q={quote_plus(query)}",
                wait_until="networkidle",
                timeout=_NAV_TIMEOUT_MS,
            )
        except (TimeoutError, PlaywrightTimeoutError):
            # audit M-28: Playwright raises its OWN TimeoutError, which is NOT a
            # subclass of asyncio.TimeoutError -- the old handler was a dead
            # branch and networkidle on a Bing page times out routinely. A
            # partial render is still usable, so fall through to what landed.
            pass
        items = await page.locator("#b_results > li.b_algo").all()
        if not items:
            return f"No results found for {query!r}."
        lines = []
        for i, item in enumerate(items[:num_results], start=1):
            link = item.locator("h2 a").first
            if await link.count() == 0:
                continue
            title = (await link.inner_text()).strip() or "(untitled)"
            href = await link.get_attribute("href") or ""
            url = _decode_bing_redirect(href)
            snippet_el = item.locator(".b_caption p, .b_snippet, .b_lineclamp2, .b_lineclamp3, .b_lineclamp4")
            snippet = (await snippet_el.first.inner_text()).strip() if await snippet_el.count() else ""
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}".rstrip())
        return "\n\n".join(lines) if lines else f"No results found for {query!r}."


async def _run_browse_page(url: str, want_screenshot: bool, question: str) -> str:

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    # SSRF guard (agent/tools/url_guard.py). Two layers, because the entry
    # check alone is not enough: Playwright follows redirects internally, so
    # a public URL that 302s to 127.0.0.1 or 169.254.169.254 would never come
    # back through here. The route guard re-checks every request the page
    # makes -- initial navigation, each redirect hop, and subresources.
    try:
        await assert_public_url(url)
    except UnsafeUrlError as e:
        return f"ERROR: {e}"

    blocked: list[str] = []

    async with _browser_page() as page:
        await page.route("**/*", make_route_guard(lambda u, why: blocked.append(f"{u} ({why})")))
        try:
            await page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
        except (TimeoutError, PlaywrightTimeoutError):
            # audit M-28: catch Playwright's own TimeoutError too (not an
            # asyncio.TimeoutError subclass), otherwise this was a dead branch.
            return f"ERROR: timed out loading {url!r} (over {_NAV_TIMEOUT_MS // 1000}s)"
        except Exception as e:  # noqa: BLE001 -- bad URL, DNS failure, refused connection, etc.
            return f"ERROR: failed to load {url!r}: {e}"

        if blocked and not page.url.startswith(("http://", "https://")):
            return f"ERROR: blocked redirect to a non-public address -- {blocked[0]}"

        title = await page.title()
        try:
            text = (await page.inner_text("body")).strip()
        except Exception:
            text = ""
        if len(text) > _PAGE_TEXT_CAP:
            text = text[:_PAGE_TEXT_CAP] + f"\n... [truncated, {len(text) - _PAGE_TEXT_CAP} more chars]"

        parts = [f"# {title or url}", f"URL: {page.url}", "", text or "(no visible text extracted)"]

        if want_screenshot:
            png_bytes = await page.screenshot(type="png")
            try:
                description = await describe_image_bytes(
                    png_bytes,
                    "image/png",
                    question.strip() or (
                        "Describe this webpage's visual design for a UI/UX planning conversation: "
                        "layout, color palette, typography style, spacing, and any notable UI patterns."
                    ),
                )
                parts.append("\n## Visual description (screenshot)\n" + description)
            except Exception as e:  # noqa: BLE001 -- text extraction above still succeeded, don't lose that
                parts.append(f"\n## Visual description (screenshot)\nERROR: vision call failed: {e}")

        return "\n".join(parts)


_OWN_SPACE_PREFIXES = ("/skills/", "/memories/", "/org-memory/", "/episodes/")


def _own_space_redirect(path: str) -> str | None:
    """A path into the agent's OWN file space aimed at the repo tools gets a
    redirect naming the right tool, not a generic path error. Observed live
    2026-08-28: the prompt says to read /skills/codebase-map/SKILL.md with
    built-in read_file, the model called read_project_file with it instead,
    and the escape-guard's "paths must be RELATIVE" reply sent it hunting for
    a relative spelling of a file that was never in the repo at all."""
    # Absolute-prefixed only: a leading-slash own-space path is never a valid
    # repo path (repo paths are relative), so the redirect is unambiguous. A
    # RELATIVE "skills/..." is left alone -- a repo can legitimately contain
    # a top-level skills/ directory of its own.
    for prefix in _OWN_SPACE_PREFIXES:
        if path.startswith(prefix):
            return (
                f"ERROR: {path!r} is in YOUR OWN file space, not the repo -- this tool only reads "
                f"repo files. Call your built-in read_file tool with file_path='{path}' "
                f"instead (same path, different tool)."
            )
    return None


def _reread_note(path: str, seen: int) -> str:
    """Warn before the cap bites, and say what to do instead.

    The model cannot see that its earlier reads were summarized away, so from
    inside the loop each re-read looks like a first read. Naming the count is
    the only signal it gets that it is going in circles."""
    if seen < _READ_WARN_AT:
        return ""
    return (
        f"\n\n[You have now read {path!r} {seen} times in this turn. Older tool results get "
        f"summarized out of context; your own written text does not. Record what this file told "
        f"you in your next reply instead of reading it again -- after {_READ_CAP} reads this tool "
        f"stops returning it.]"
    )

def _project_root(repo: str, allowed_repos: list[str] | None = None) -> str:
    if repo not in PROJECTS:
        raise ValueError(f"unknown repo {repo!r} -- must be one of {list(PROJECTS)}")
    # audit H-2: the repo argument is model-chosen. Without this check a
    # restricted user could open a session on a repo they ARE allowed, then
    # ask the model to read a file from one they are NOT -- the allow-list the
    # auth module documents as a per-user control was silently overridden. None
    # means "no restriction" (a full-access operator), an explicit list gates.
    if allowed_repos is not None and repo not in allowed_repos:
        raise ValueError(f"access to repo {repo!r} is not permitted for this session")
    return PROJECTS[repo]["sandbox"]


def make_planning_tools(existing_plan: str | None = None, allowed_repos: list[str] | None = None) -> tuple[list, dict]:
    """Returns ([web_search, browse_page, list_project_dir, read_project_file,
    save_plan], plan_ref).

    Unlike agent_tools.py's read/list tools (closure-bound to one repo_root
    at construction time, matching a build task's single-repo scope),
    list_project_dir/read_project_file take `repo` as an explicit argument --
    planning is deliberately cross-project: comparing how two of this
    operator's projects each solve something, or pulling a pattern from one
    into a plan for another, is real, common planning-conversation value that
    a single-repo-scoped tool can't provide at all.

    `plan_ref` is a mutable dict (`{"markdown": str | None}`) the caller
    reads back after each turn -- same pattern as agent_tools.py's
    last_failed_edit_ref: the tool's closure is the only thing that can see
    the model's save_plan call as it happens, so the result has to come back
    through a shared mutable reference, not a return value (tools return
    strings to the model, not structured data to the caller).
    """
    # Per-TURN read ledger: make_planning_tools is called once per turn (see
    # build_planning_agent), so this counts reads within a single turn and
    # resets naturally on the next one.
    read_counts: dict[tuple[str, str], int] = {}

    # Seeded with whatever the session already has saved. A planning agent is
    # rebuilt from scratch on EVERY turn, so a plan_ref that always started at
    # None meant the session's own draft was invisible to the turn that came
    # after it -- and, worse, got clobbered (see run_planning_turn's caller).
    plan_ref: dict = {"markdown": existing_plan}

    @tool
    @tool_errors_to_text
    async def web_search(query: str, num_results: int = 6) -> str:
        """Search the web (Bing) for research, documentation, competitor examples,
        or design/UX inspiration. Returns up to `num_results` results, each with a
        title, real URL, and snippet. Use browse_page on a promising URL to read
        the full page or see what it actually looks like."""
        num_results = max(1, min(num_results, _SEARCH_RESULT_CAP))
        try:
            return await _run_web_search(query, num_results)
        except Exception as e:  # noqa: BLE001 -- surfaced to the model as a tool result, not raised
            return f"ERROR: web search failed: {e}"

    @tool
    @tool_errors_to_text
    async def browse_page(url: str, screenshot: bool = False, question: str = "") -> str:
        """Load a real webpage (JS-rendered, via a real headless browser) and
        extract its visible text. Pass `screenshot=True` to ALSO get a visual
        description of how the page actually looks (layout, colors, typography,
        UI patterns) -- use this whenever the user is asking about a site's design
        or you want to reference/compare a competitor's UI. `question` narrows
        either the text reading or the visual description to something specific."""
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"ERROR: {url!r} is not a valid http(s) URL"
        try:
            return await _run_browse_page(url, screenshot, question)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: browse_page failed: {e}"

    @tool
    @tool_errors_to_text
    def list_project_dir(repo: str, path: str = ".") -> str:
        """List files and subdirectories at `path` (repo-relative, e.g. "src" or
        "." for the repo root) within `repo` -- one of the operator's configured
        projects, not necessarily the one this session is about. Read-only, for
        getting oriented in a project (this session's own, or another one for
        comparison/reference) before planning changes to it."""
        redirect = _own_space_redirect(path)
        if redirect:
            return redirect
        try:
            repo_root = _project_root(repo, allowed_repos)
        except ValueError as e:
            return f"ERROR: {e}"
        try:
            target = _resolve(repo_root, path)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        if not target.exists():
            return f"ERROR: {path!r} does not exist in {repo!r}"
        if not target.is_dir():
            return f"ERROR: {path!r} is a file, not a directory -- use read_project_file to view it"
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError as e:
            # Not reachable via the .exists()/.is_dir() checks above (both
            # answer False rather than raising), but a directory can still be
            # unreadable at the moment we open it -- permissions, a mount that
            # went away mid-session. Name the repo-relative path, never the
            # host one (see _resolve's own comment in files.py).
            return f"ERROR: cannot list {path!r} in {repo!r}: {e.strerror or e}"
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
        return "\n".join(lines) if lines else "(empty directory)"

    @tool
    @tool_errors_to_text
    def read_project_file(repo: str, path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a file (repo-relative path) from `repo` -- one of the operator's
        configured projects, not necessarily the one this session is about.
        Read-only. Use this to understand an existing codebase/design (this
        session's own project, or another one to compare/borrow a pattern from)
        before proposing changes to it.

        For LARGE files, page through THIS tool: `offset` is the 1-based line
        to start from, `limit` the number of lines (e.g. offset=600,
        limit=600). A plain read of a big file returns its beginning plus the
        line count -- follow up with offset/limit rather than asking for the
        whole file again, which just returns the identical text."""
        redirect = _own_space_redirect(path)
        if redirect:
            return redirect
        try:
            repo_root = _project_root(repo, allowed_repos)
        except ValueError as e:
            return f"ERROR: {e}"
        # Counted before the read, so a refusal costs nothing. Keyed on the
        # file rather than the exact window: the loop this exists to stop
        # paged through the same file with VARYING offsets, so a same-window
        # check would not have caught it.
        seen = read_counts[(repo, path)] = read_counts.get((repo, path), 0) + 1
        if seen > _READ_CAP:
            return (
                f"ERROR: you have already read {path!r} in {repo!r} {seen - 1} times this turn. "
                f"Re-reading it is not producing new information, and this tool will not return it "
                f"again. If you are re-reading because earlier results dropped out of context, that "
                f"will keep happening -- WRITE what you learned into your reply as you go, because "
                f"your own written conclusions survive summarization and raw tool output does not. "
                f"Answer with what you have, or read a file you have not read yet."
            )

        try:
            content = read_file(repo_root, path, max_chars=_READ_HARD_CAP_CHARS)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        except BinaryFileError as e:
            return f"ERROR: {e}"
        except FileNotFoundError:
            return f"ERROR: {path!r} does not exist in {repo!r}"
        except IsADirectoryError:
            return (
                f"ERROR: {path!r} is a directory in {repo!r}, not a file -- "
                f"use list_project_dir to see what's inside it"
            )
        except NotADirectoryError:
            # The live one: every sandbox is a git worktree, whose `.git` is a
            # one-line pointer FILE, so `.git/HEAD` (a reasonable-looking guess
            # for "what branch is this on?") resolves THROUGH a file. Say that
            # plainly -- the raw errno text sends the model looking for a
            # missing file that is actually right there.
            return (
                f"ERROR: {path!r} cannot be read from {repo!r} -- a directory in that path is "
                f"actually a file (a git worktree's .git is a pointer file, not a directory, so "
                f"nothing under .git/ is readable this way)"
            )
        except OSError as e:
            return f"ERROR: cannot read {path!r} in {repo!r}: {e.strerror or e}"

        # Paging exists here for the same reason it does on agent_tools.read: a
        # model's instinct when a result comes back truncated is to ask for the
        # file again, and without a way to page that returns the IDENTICAL
        # truncated text forever. The planning agent has no bash to fall back
        # on -- read_project_file is its only route into a repo -- so a file
        # bigger than the cap was simply unreachable past its first 40k chars.
        # Confirmed live 2026-08-27: a session hit this on a 74_703-char
        # strategy file, reported "the file keeps truncating at the same
        # point", and escalated into subagent investigations to get around it.
        if offset or limit:
            lines = content.split("\n")
            start = max(0, (offset or 1) - 1)
            count = limit if limit and limit > 0 else 400
            slice_lines = lines[start:start + count]
            if not slice_lines:
                return f"(no lines at offset {offset} -- {path!r} has {len(lines)} lines)"
            numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(slice_lines))
            remaining = len(lines) - (start + len(slice_lines))
            footer = f"\n\n[lines {start + 1}-{start + len(slice_lines)} of {len(lines)}" + (
                f"; {remaining} more after this]" if remaining > 0 else "]"
            )
            return numbered + footer + _reread_note(path, seen)

        if len(content) > _READ_INLINE_CAP_CHARS:
            head = content[:_READ_INLINE_CAP_CHARS]
            shown = head.count("\n") + 1
            total_lines = content.count("\n") + 1
            # Say plainly that repeating the call is pointless and give the
            # exact next call to make -- a bare "[truncated]" marker is what
            # the model kept walking into.
            return (
                f"{head}\n\n[TRUNCATED. {path!r} is {len(content)} chars / {total_lines} lines; "
                f"you have seen lines 1-{shown}. Reading it again the same way returns this SAME "
                f"text -- to see the rest, call read_project_file with offset/limit, e.g. "
                f"read_project_file(repo={repo!r}, path={path!r}, offset={shown}, limit=800). Use "
                f"BIG windows; 50-100 line slices just loop.]"
            ) + _reread_note(path, seen)
        return content + _reread_note(path, seen)

    @tool
    @tool_errors_to_text
    def save_plan(markdown: str) -> str:
        """Save (or replace) the current draft plan document, in Markdown. Call
        this whenever the plan is ready or has meaningfully changed -- this is
        what the user's "Build Now" button hands off to the build system, so it
        should be a complete, self-contained spec a builder could act on without
        this conversation's context: goal, key requirements/decisions gathered so
        far, and any design/UX direction. You can call this multiple times as the
        plan evolves; each call replaces the previous draft."""
        plan_ref["markdown"] = markdown
        return "Plan saved. The user can now see it and use \"Build Now\" whenever they're ready."

    return [web_search, browse_page, list_project_dir, read_project_file, save_plan], plan_ref
