"""LangSmith tracing setup -- installs a redacting Client so trace payloads
sent to LangSmith's servers never carry real secrets, and wires per-task
metadata so a specific task's trace is actually findable in the UI.

Found missing during a follow-up audit of the LangSmith addition itself
(distinct from the earlier full LangGraph/deepagents docs audit): env-var-
only tracing (LANGSMITH_TRACING=true/LANGSMITH_API_KEY) is genuinely
zero-code to turn ON, but it ships every trace payload -- full prompts,
tool call args, and tool call RESULTS -- to LangSmith's servers verbatim,
with no redaction by default. This system's agent-facing `bash` tool can
read arbitrary files inside its sandbox (see agent/tools/sandbox.py); the
INTERRUPT_ON human-in-the-loop gate (deep_agent.py) catches a risky call
before it runs based on PATH/COMMAND patterns, but that's a different
control than this one -- it doesn't know or care what a tool's OUTPUT
contains, so a file that doesn't match any of INTERRUPT_ON's sensitive-path
markers (a config file with an unexpected name, a secret embedded in an
otherwise-ordinary file) could still have its contents echoed straight into
a ToolMessage, and therefore straight into a trace. This module is the
actual backstop for that: regardless of INTERRUPT_ON, nothing shaped like a
real credential should leave this process in a trace payload.

`langsmith.configure(client=...)` (not a bespoke Client passed around
explicitly) is the documented mechanism for this: LangChain's own
env-var-triggered auto-tracing resolves its Client through `langsmith.
run_trees.get_cached_client()`, a lazily-constructed process-wide singleton
-- `configure()` is the one public, documented way to install a
pre-configured Client into that same slot before anything else touches it,
confirmed by reading get_cached_client()'s own source directly (langsmith/
run_trees.py) rather than assuming the docs' one-line description was the
whole story.
"""

import re

_REDACTED = "[REDACTED]"

# Deliberately over-inclusive, not surgical -- a false-positive redaction
# (some ordinary-looking string gets replaced with [REDACTED] in a trace)
# costs a moment of debugging friction; a false negative (a real secret
# reaches LangSmith's servers) costs a real credential. Two complementary
# strategies:
#   1. KEY-NAME-based: redact the VALUE half of anything that LOOKS like
#      key: value / key=value where the key name suggests secrecy,
#      regardless of what the value itself looks like -- catches secrets in
#      formats we didn't anticipate.
#   2. VALUE-SHAPE-based: redact specific, high-confidence credential
#      formats (this proxy's own sk-/lsv2_ key shapes, AWS keys, JWTs, PEM
#      private key blocks, DSNs with embedded passwords, bearer tokens) even
#      when the surrounding key name gives no hint at all (e.g. a raw
#      connection string pasted into an otherwise ordinary-looking file).
_RULES = [
    # key: value / key=value where the key name suggests secrecy.
    #
    # The prefix is `[A-Za-z0-9_.-]*`, NOT `\b`. `_` is a word character, so a
    # leading \b anchor cannot match inside a namespaced identifier -- which
    # is what essentially every real env var is. Confirmed 2026-08-24: bare
    # `API_KEY=` redacted correctly while `OPENROUTER_API_KEY=`,
    # `DOBA_PRIVATE_KEY=`, `SMTP_PASS=` and `MY_SECRET=` all sailed through
    # in the clear. The rule was effectively dead for real-world names.
    #
    # `pass` is included alongside password/passwd for the same reason
    # (SMTP_PASS, DB_PASS), and `session`/`cookie` because a session token is
    # a live credential -- one appearing in a tool result is enough to
    # impersonate the operator.
    {
        "pattern": re.compile(
            r'(?i)[A-Za-z0-9_.-]*('
            r'api[_-]?key|password|passwd|pass|secret|token|credential|'
            r'auth[_-]?token|access[_-]?key|private[_-]?key|session|cookie'
            r')'
            # Trailing segments, but only underscore/hyphen-delimited ones, so
            # AUTH_SECRET_KEY matches (SECRET + "_KEY") while a plain English
            # plural like "tokens: 1500" does not (the "s" is not delimited).
            r'(?:[_-][A-Za-z0-9]+)*'
            r'\s*[:=]\s*["\']?([^\s"\',}\]]{4,})["\']?'
        ),
        "replace": rf"\1: {_REDACTED}",
    },
    # DSNs/connection strings with an embedded password
    # (postgresql://user:pass@host, redis://:pass@host, etc.)
    {
        "pattern": re.compile(r"(?i)\b(postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?)://[^:/\s]*:[^@/\s]+@"),
        "replace": rf"\1://{_REDACTED}@",
    },
    # This proxy's own LiteLLM key shape (sk-...) and OpenAI-compatible keys generally.
    {"pattern": re.compile(r"\bsk-[A-Za-z0-9\-_]{16,}\b"), "replace": f"sk-{_REDACTED}"},
    # LangSmith's own key shape -- redact it too, so a key never traces
    # itself. Underscore included in the char class: the real key format
    # (lsv2_pt_<32-hex>_<12-hex>) has an underscore separator partway
    # through the suffix, so [A-Za-z0-9] alone would stop the match at that
    # underscore and the trailing \b would never match (word-char to
    # word-char is not a boundary) -- the whole match would silently fail,
    # not just a partial redaction.
    {"pattern": re.compile(r"\blsv2_(?:pt|sk)_[A-Za-z0-9_]{16,}\b"), "replace": f"lsv2_{_REDACTED}"},
    # AWS access key IDs.
    {"pattern": re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "replace": f"AKIA{_REDACTED}"},
    # JWTs (header.payload.signature, base64url segments).
    {
        "pattern": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "replace": f"eyJ{_REDACTED}",
    },
    # PEM private key blocks, whole block regardless of key type.
    {
        "pattern": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
        "replace": f"-----BEGIN PRIVATE KEY----- {_REDACTED} -----END PRIVATE KEY-----",
    },
    # Authorization: Bearer <token> headers appearing in any traced text.
    {
        "pattern": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.=]{10,}"),
        "replace": f"Bearer {_REDACTED}",
    },
]


def _build_anonymizer():
    from langsmith.anonymizer import create_anonymizer

    return create_anonymizer(_RULES)


def install_langsmith(config) -> None:
    """Call once at process startup (server.py's lifespan). No-ops cleanly
    if tracing isn't actually enabled for this deployment -- never
    constructs a Client (which would eagerly validate an API key) unless
    LANGSMITH_TRACING is genuinely on, so this is safe to call unconditionally.
    """
    if not config.langsmith_tracing:
        return

    import langsmith as ls

    ls.configure(client=ls.Client(anonymizer=_build_anonymizer()))
