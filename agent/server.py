"""FastAPI backend -- task creation/listing/state, WebSocket streaming of a
running task's graph execution. Checkpointer + store are opened once at
startup and shared for the app's lifetime (both are long-lived async context
managers, matching how langgraph-checkpoint-postgres expects to be used --
opening a fresh connection per request would be wasteful and race-prone).
"""

import asyncio
import json
import logging
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

logger = logging.getLogger("3d-agent")

import httpx
from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg import OperationalError as PgOperationalError
from pydantic import BaseModel, Field

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

from agent import cartographer
from agent import paths
from agent import rate_limit
from agent.config import PROJECTS, load_config
from agent import env_config
from agent.observability import install_langsmith
from agent.outer_graph import build_outer_graph, initial_state, open_checkpointer, open_store, project_lock
from agent.messages import add_message
from agent.tools.model_rates import warm_rates
from agent import model_config
from agent.model_config import resolve_alias
from agent.classify import classify_task, TaskClassification, TEST_REMINDER_NOTE
from agent.planning_chat import build_planning_agent, classify_planning_difficulty, planning_thread_config, run_planning_turn, _translate_message as _translate_planning_message
from agent import auth
from agent.auth import SESSION_COOKIE_NAME, User, check_repo_access
from agent.notify import notify_operators_bg, send_telegram, task_alert, watch_services

config = load_config()
install_langsmith(config)  # no-ops cleanly if LANGSMITH_TRACING isn't set -- see observability.py

# task_id -> list of subscriber queues, for fanning live updates out to every
# connected WS client (a reconnect or a second browser tab both just get a
# new queue and the same event stream from that point forward).
_subscribers: dict[str, list[tuple[asyncio.Queue, WebSocket]]] = {}
_running_tasks: dict[str, asyncio.Task] = {}

# Same fan-out pattern, kept in its own dicts (not reusing the task ones
# above) -- a planning session_id and a task_id are both plain strings with
# no shared namespace, and keeping them separate avoids ever having to
# reason about whether an id collision between the two is possible.
_planning_subscribers: dict[str, list[tuple[asyncio.Queue, WebSocket]]] = {}
_running_planning_turns: dict[str, asyncio.Task] = {}


def _log_warm_rates_failure(task: "asyncio.Task") -> None:
    """C-1: surface a warm_rates() startup failure instead of swallowing it.
    A failed rate warm means the hard budget ceiling has no data and would
    fail every task's first model call -- that must be visible in the log, not
    silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("CRITICAL: model-rate warm failed -- the budget ceiling has no rates: %r", exc)


async def _auto_resume_orphaned_tasks(startup_delay: float = 5.0) -> None:
    """Reconnect tasks orphaned by a restart, without operator action.

    resume_task's own docstring declares restarts routine ("this pm2 process
    gets restarted routinely to deploy fixes") and its orphan branch resumes
    from the checkpoint with no replanning -- but nothing ever CALLED that
    path automatically, so every deploy stranded any in-flight build until a
    human noticed the silence and clicked Resume (2026-08-27: the operator
    watched a stalled screener build for an hour and asked why). This is the
    missing last mile: at startup, find every task whose Store status says
    "running" while nothing drives it, and restart its driver exactly the
    way the endpoint's orphan branch does (same +40 iteration headroom, no
    budget change -- the operator already approved this task's budget).

    Deliberately NOT resumed:
      - escalated tasks (they are waiting for a human by design),
      - "stopped" (the operator chose that),
      - a task that made NO checkpoint progress since its last auto-resume
        (auto_resume_ckpt marker): if resuming it did nothing once, a boot
        loop of retries would amplify a poison task instead of surfacing it.
        It stays orphaned for a human, exactly as before this existed.
    """
    await asyncio.sleep(startup_delay)  # let startup settle; not correctness-critical
    graph = app.state.graph
    for repo in PROJECTS:
        try:
            items = await app.state.store.asearch(("tasks", repo), limit=50)
        except Exception:  # noqa: BLE001 -- one repo's failure must not strand the others
            logger.exception("auto-resume: task scan failed for %s", repo)
            continue
        for item in items:
            meta = item.value
            task_id = meta.get("task_id") or item.key
            if meta.get("status") != "running" or task_id in _running_tasks:
                continue
            try:
                thread_config = {"configurable": {"thread_id": task_id}}
                checkpoint = await graph.aget_state(thread_config)
                values = checkpoint.values if checkpoint else None
                if not values or values.get("escalated"):
                    continue
                # Progress marker: outer checkpoint id PLUS the inner work
                # thread's latest checkpoint ts. The outer id alone is wrong
                # -- it stays frozen for an ENTIRE work pass (often 30+ min),
                # so two restarts inside one long pass would read as "no
                # progress" and wrongly strand a perfectly healthy task. The
                # inner thread checkpoints every step, so real work always
                # moves this marker.
                gen = values.get("inner_thread_generation", 0)
                inner_id = f"{task_id}:work:g{gen}" if gen else f"{task_id}:work"
                inner_ts = ""
                try:
                    inner_snap = await app.state.checkpointer.aget_tuple(
                        {"configurable": {"thread_id": inner_id}})
                    inner_ts = (inner_snap.checkpoint.get("ts") or "") if inner_snap else ""
                except Exception:  # noqa: BLE001 -- marker precision degrades, resume still works
                    logger.exception("auto-resume: inner-thread read failed for %s", task_id)
                outer_id = (checkpoint.config or {}).get("configurable", {}).get("checkpoint_id", "")
                ckpt_ts = f"{outer_id}|{inner_ts}"
                if meta.get("auto_resume_ckpt") == ckpt_ts:
                    logger.error(
                        "auto-resume: task %s made no progress since its last auto-resume -- "
                        "leaving it for the operator (possible poison task)", task_id)
                    continue
                await graph.aupdate_state(thread_config, {
                    "task_id": task_id,
                    "budget_usd": values["budget_usd"],
                    "max_iterations": values.get("max_iterations", 40) + 40,
                })
                await app.state.store.aput(("tasks", repo), task_id,
                                           {**meta, "auto_resume_ckpt": ckpt_ts})
                _running_tasks[task_id] = asyncio.create_task(
                    _stream_graph(task_id, values["repo"], values["goal"], values["budget_usd"], None)
                )
                logger.warning("auto-resume: reconnected orphaned task %s (%s) after restart", task_id, repo)
                _notify_bg(task_alert(
                    "auto_resumed", repo, values.get("goal", ""), values.get("cost_so_far"),
                    "The server restarted mid-run; the task reconnected automatically and is working again."))
            except Exception:  # noqa: BLE001 -- one task's failure must not strand the others
                logger.exception("auto-resume: failed to reconnect task %s", task_id)


async def _drain_planning_turns(timeout: float = 15.0) -> None:
    """Cancel every in-flight planning turn and WAIT for its teardown.

    Called from lifespan shutdown while the store/checkpointer pools are
    still open -- the whole point. The tasks' own CancelledError handlers do
    the actual banking; this just guarantees they get to run against a live
    pool instead of racing process exit.
    """
    tasks = list(_running_planning_turns.values())
    if not tasks:
        return
    logger.info("shutdown: draining %d in-flight planning turn(s)", len(tasks))
    for t in tasks:
        t.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:  # pragma: no cover -- only a wedged teardown lands here
        logger.error("shutdown: planning turn %r did not tear down within %.0fs", t.get_name(), timeout)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with open_checkpointer(config) as checkpointer, open_store(config) as store, auth.open_auth_pool(config) as auth_pool:
        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.auth_pool = auth_pool
        generated_password = await auth.seed_admin_if_none(auth_pool, config.admin_email)
        if generated_password:
            # Only ever printed once, the very first time this deployment
            # has zero users -- must_change_password=True forces a real
            # password to replace this on first login, so it's not a
            # standing secret sitting in the log after that.
            logger.warning(
                "Seeded initial admin account %s with generated password: %s "
                "(must be changed on first login)", config.admin_email, generated_password,
            )
        # Both checkpointer and store passed to .compile() -- store isn't
        # actually read via LangGraph's own node-kwarg injection here (see
        # outer_graph.py's own comment: work_node/verify_and_ship_node use
        # app_config/pg_store, bound directly via functools.partial,
        # specifically to avoid that injection path), but passing it is
        # still correct/harmless and keeps this graph object consistent with
        # idiomatic LangGraph usage for anything else that might introspect it.
        app.state.graph = build_outer_graph(config, checkpointer, store).compile(
            checkpointer=checkpointer, store=store
        )
        # Pre-warm the model-usage cache in the background so the first
        # Analytics page load after a restart doesn't pay the cold
        # LangSmith scan inline (see get_model_usage's own docstring).
        warm_task = asyncio.create_task(_refresh_model_usage())
        # Same reasoning, different cache: model_rates.estimate_cost's rate
        # table load includes a synchronous network call to OpenRouter's
        # pricing endpoint -- pre-warming it here means that blocking call
        # happens in a background thread before any real task needs a cost
        # estimate, not inline on the event loop the first time one does.
        # C-1: warm_rates() reads llm-router/config.yaml; if that raises (bad
        # path, malformed yaml) the budget ceiling silently does not exist.
        # A bare create_task swallows the exception, so attach a done-callback
        # that surfaces it loudly at startup instead of at every task's first
        # model call.
        rates_warm_task = asyncio.create_task(warm_rates())
        rates_warm_task.add_done_callback(_log_warm_rates_failure)
        # Same reasoning again for the tool-call reliability scan.
        tool_reliability_warm_task = asyncio.create_task(_refresh_tool_reliability())
        # Same reasoning again for the trace-summary scan. Token totals in
        # its result read from _model_usage_cache, which may still be empty
        # the very first time this runs concurrently with warm_task above --
        # harmless, the next 600s refresh picks up real numbers once
        # model-usage has actually warmed.
        trace_summary_warm_task = asyncio.create_task(_refresh_trace_summary())
        # Reconnect any build task a restart orphaned -- see the function's
        # own docstring. Backgrounded so startup never blocks on it.
        auto_resume_task = asyncio.create_task(_auto_resume_orphaned_tasks())
        # Service-restart alerts (operator request 2026-08-28): a router or
        # bot restarting mid-task is exactly the kind of event that used to
        # be discovered by watching a silent screen. The agent backend
        # itself is excluded from the poll (its restart resets this watcher)
        # and announces itself with the startup line below instead.
        service_watch_task = asyncio.create_task(watch_services(auth_pool))
        notify_operators_bg(auth_pool, "🔄 agent backend restarted (deploys land this way; "
                            "orphaned tasks auto-resume, planning turns re-send)")
        yield
        service_watch_task.cancel()
        auto_resume_task.cancel()
        # Drain in-flight planning turns BEFORE this `async with` block exits
        # and closes the Postgres pools. Left to the runtime, these tasks are
        # cancelled during asyncio.run() cleanup -- AFTER the pools are gone --
        # so the cancel-path teardown that banks the turn's plan/cost/title
        # (_bank_planning_turn) dies on its own store write. Confirmed live
        # 2026-08-27: a pm2 restart during a planning turn logged "failed to
        # persist planning progress" from exactly that ordering, and the
        # session's real spend (router-billed) was lost from the ledger.
        # Cancelling here, while the pools are still open, lets each turn's
        # CancelledError handler finish its banking write. The 15s ceiling is
        # generous -- banking is one store read + one write.
        await _drain_planning_turns(timeout=15.0)
        warm_task.cancel()
        rates_warm_task.cancel()
        tool_reliability_warm_task.cancel()
        trace_summary_warm_task.cancel()


app = FastAPI(lifespan=lifespan)
# CORS was allow_origins=["*"] with a comment claiming nginx tightened it in
# production. nginx sets no CORS headers at all, so nothing did -- the comment
# described a control that did not exist. Real exposure was limited (credentials
# were never allowed, and the session cookie is SameSite=strict so it is not
# sent cross-site anyway), but a wildcard on an authenticated app is not
# something to leave sitting behind a false comment.
#
# This app serves its own frontend, so same-origin requests do not use CORS at
# all and the correct production value is "no origins". Only a split dev setup
# (Vite on its own port) needs any, via CORS_ALLOW_ORIGINS.
if config.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# audit M-9: response security headers (defence-in-depth behind React's escaping,
# on an app that renders model-produced text throughout). CSRF still rests on the
# SameSite=strict session cookie -- these add the layers a grep found entirely
# missing. style-src allows 'unsafe-inline' because the Vite/React build emits
# inline styles; connect-src 'self' covers the same-origin REST + WebSocket.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


from starlette.responses import JSONResponse as _JSONResponse


@app.middleware("http")
async def _body_size_limit(request, call_next):
    # audit M-13: reject an over-large request by its Content-Length before the
    # body is read, so no endpoint (uploads OR unbounded JSON goal/message/
    # attachments) can be handed an arbitrarily large body. The ceiling is sized
    # for the maximum legitimate upload batch; JSON bodies sit far below it.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > REQUEST_BODY_MAX_BYTES:
                return _JSONResponse(
                    {"detail": f"request body exceeds {REQUEST_BODY_MAX_BYTES // (1024*1024)}MB"},
                    status_code=413,
                )
        except ValueError:
            return _JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ---------------------------------------------------------------------------
# Auth -- username/password + TOTP 2FA (agent/auth.py). Every route below
# this point that touches a repo-scoped resource calls check_repo_access
# explicitly once `repo` is known (never uniform enough in shape -- query
# param, request body field, or an existing task/session's own stored repo
# -- for one FastAPI dependency to cover safely). Analytics and Model
# Configuration are admin-only outright (require_admin), not repo-scoped --
# they're operator/global concerns (aggregate spend across every project,
# which LLM model each pinned role uses), not something a restricted
# per-project account should see or change.
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class Verify2FARequest(BaseModel):
    temp_token: str
    code: str


class Setup2FARequest(BaseModel):
    password: str | None = None


class Confirm2FARequest(BaseModel):
    code: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str
    allowed_repos: list[str] | None = None
    auto_approve_commands: bool = False


class UpdateAutoApproveRequest(BaseModel):
    auto_approve_commands: bool


class UpdateUserAccessRequest(BaseModel):
    allowed_repos: list[str] | None = None
    auto_approve_commands: bool | None = None


def _user_public(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "allowed_repos": user.allowed_repos, "totp_enabled": user.totp_enabled,
        "must_change_password": user.must_change_password,
        "require_totp_setup": user.role == "admin" and not user.totp_enabled,
        "auto_approve_commands": user.auto_approve_commands,
        "require_merge_review": user.require_merge_review,
    }


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME, token, max_age=auth.SESSION_TTL_SECONDS,
        httponly=True, samesite="strict", secure=True, path="/",
    )
def _forced_screen_block(user: User) -> str | None:
    """Return a reason string if `user` is parked behind a forced screen, else
    None. Factored out (audit H-1) so the WebSocket handlers enforce the exact
    same two gates as require_full_auth -- previously they authenticated only
    the session and let a must-change-password / no-2FA account open the live
    task and planning streams and watch tool-call arguments and results.
    """
    if user.must_change_password:
        return "password change required before using this"
    if user.role == "admin" and not user.totp_enabled:
        return "2FA setup required before using this"
    return None


async def require_full_auth(user: User = Depends(auth.get_current_user)) -> User:
    # Both forced-screen flags are enforced server-side here, not only via
    # the frontend routing app.tsx does for the
    # same two flags -- a session cookie alone would otherwise be enough to
    # reach every real endpoint directly, skipping both forced screens
    # entirely (the same "nginx allows all IPs" reasoning that makes
    # frontend-only gating unsafe applies here too). 2FA is admin-only
    # (never mandatory for a role="user" account, see agent/auth.py's own
    # module docstring); a temporary/generated password must always be
    # replaced before anything else, regardless of role.
    blocked = _forced_screen_block(user)
    if blocked:
        raise HTTPException(403, blocked)
    return user


@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response, request: Request):
    rate_limit.check_rate_limit(request, "login")  # audit H-7
    row = await auth.get_user_by_email(app.state.auth_pool, req.email.strip().lower())
    # audit M-1: run argon2 on both branches so an unknown email takes the same
    # time as a real one (no user-enumeration timing oracle).
    if not row:
        auth.verify_password_absent()
        raise HTTPException(401, "invalid email or password")
    if not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    rate_limit.clear_rate_limit(request, "login")
    if row["totp_enabled"]:
        temp_token = await auth.create_pending_2fa(app.state.auth_pool, row["id"])
        return {"requires_2fa": True, "temp_token": temp_token}
    token = await auth.create_session(app.state.auth_pool, row["id"])
    _set_session_cookie(response, token)
    return {"requires_2fa": False, "user": _user_public(auth._row_to_user(row))}


@app.post("/api/auth/2fa/verify")
async def verify_2fa(req: Verify2FARequest, response: Response, request: Request):
    rate_limit.check_rate_limit(request, "verify-2fa")  # audit H-7
    pending = await auth.resolve_pending_2fa(app.state.auth_pool, req.temp_token)
    if not pending:
        raise HTTPException(401, "2FA challenge expired -- log in again")
    ok = await auth.verify_totp_or_recovery(app.state.auth_pool, config, pending["user_id"], req.code.strip())
    if not ok:
        raise HTTPException(400, "invalid code")
    token = await auth.create_session(app.state.auth_pool, pending["user_id"])
    _set_session_cookie(response, token)
    row = await auth.get_user_by_id(app.state.auth_pool, pending["user_id"])
    return {"user": _user_public(auth._row_to_user(row))}


@app.post("/api/auth/logout")
async def logout(response: Response, agent_session: str | None = Cookie(default=None)):
    if agent_session:
        await auth.revoke_session(app.state.auth_pool, agent_session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    rate_limit.check_rate_limit(request, "reset-request")  # audit H-7
    # Always {"ok": true} regardless of whether the email matches a real
    # account -- auth.request_password_reset itself silently no-ops for an
    # unknown email; the point is not letting the response tell an attacker
    # which emails are registered users.
    try:
        await auth.request_password_reset(app.state.auth_pool, config, req.email)
    except Exception:  # noqa: BLE001 -- an SMTP hiccup must not turn into "this email doesn't exist" info leakage either
        logger.exception("password reset email failed to send for %s", req.email)
    return {"ok": True}


@app.post("/api/auth/reset-password")
async def reset_password_endpoint(req: ResetPasswordRequest, request: Request):
    rate_limit.check_rate_limit(request, "reset-password")  # audit H-7
    error = auth.validate_password_strength(req.new_password)
    if error:
        raise HTTPException(400, error)
    ok = await auth.reset_password(app.state.auth_pool, req.email, req.code, req.new_password)
    if not ok:
        raise HTTPException(400, "invalid or expired code")
    return {"ok": True}


@app.get("/api/auth/me")
async def get_me(user: User = Depends(auth.get_current_user)):
    return _user_public(user)


@app.post("/api/auth/change-password")
async def change_password_endpoint(req: ChangePasswordRequest, user: User = Depends(auth.get_current_user)):
    row = await auth.get_user_by_id(app.state.auth_pool, user.id)
    if not auth.verify_password(req.current_password, row["password_hash"]):
        raise HTTPException(401, "current password is incorrect")
    error = auth.validate_password_strength(req.new_password)
    if error:
        raise HTTPException(400, error)
    await auth.change_password(app.state.auth_pool, user.id, req.new_password)
    return {"ok": True}


@app.post("/api/auth/2fa/setup")
async def setup_2fa(req: Setup2FARequest = Setup2FARequest(), user: User = Depends(auth.get_current_user)):
    # audit H-3: start_totp_setup clears totp_enabled as it writes the new
    # secret, so an attacker with a live session could silently DISABLE 2FA by
    # hitting this endpoint -- bypassing /2fa/disable, which explicitly refuses
    # for admins. Re-authenticate with the password before re-initiating setup
    # when 2FA is already enabled. First-time setup (2FA off) needs no password:
    # the session already proves who they are, and there is nothing to protect.
    if user.totp_enabled:
        row = await auth.get_user_by_id(app.state.auth_pool, user.id)
        if not req.password or not auth.verify_password(req.password, row["password_hash"]):
            raise HTTPException(403, "current password required to re-initialize 2FA")
    secret, uri = await auth.start_totp_setup(app.state.auth_pool, config, user.id)
    return {"secret": secret, "uri": uri}


@app.post("/api/auth/2fa/confirm")
async def confirm_2fa(req: Confirm2FARequest, user: User = Depends(auth.get_current_user)):
    codes = await auth.confirm_totp_setup(app.state.auth_pool, config, user.id, req.code.strip())
    return {"recovery_codes": codes}


@app.post("/api/auth/2fa/disable")
async def disable_2fa_endpoint(user: User = Depends(auth.get_current_user)):
    if user.role == "admin":
        raise HTTPException(403, "2FA cannot be disabled on the admin account")
    await auth.disable_totp(app.state.auth_pool, user.id)
    return {"ok": True}


class UpdateMergeReviewRequest(BaseModel):
    require_merge_review: bool


@app.post("/api/auth/me/merge-review")
async def set_own_merge_review(req: UpdateMergeReviewRequest, user: User = Depends(require_full_auth)):
    """Self-service for the same reason auto-approve is: turning the final
    look OFF removes a review the operator was doing for their own benefit,
    not a safety property someone else depends on -- the independent review
    service still gates every merge regardless. Captured onto each task at
    creation, so flipping this never changes a task already in flight."""
    await auth.update_require_merge_review(app.state.auth_pool, user.id, req.require_merge_review)
    return {"ok": True, "require_merge_review": req.require_merge_review}


@app.post("/api/auth/me/auto-approve")
async def set_own_auto_approve(req: UpdateAutoApproveRequest, user: User = Depends(require_full_auth)):
    """Self-service, deliberately not admin-only: this grants no capability
    the account doesn't already have -- every action it stops prompting for
    could be approved by hand, one at a time, by this same user today. It
    only removes the clicking. The destructive-command subset stays gated no
    matter what this is set to (see deep_agent.py's interrupt_on_for), which
    is what makes self-service reasonable rather than a way to switch off
    the safety net.
    """
    await auth.update_auto_approve(app.state.auth_pool, user.id, req.auto_approve_commands)
    return {"ok": True, "auto_approve_commands": req.auto_approve_commands}


class TelegramSettingsRequest(BaseModel):
    bot_token: str | None = None  # None/empty clears; masked sentinel keeps existing
    chat_id: str | None = None


@app.get("/api/auth/me/telegram")
async def get_telegram_settings_endpoint(user: User = Depends(require_full_auth)):
    """Masked: reports whether a token is configured, never the token."""
    return await auth.get_telegram_settings(app.state.auth_pool, user.id)


@app.post("/api/auth/me/telegram")
async def set_telegram_settings_endpoint(req: TelegramSettingsRequest, user: User = Depends(require_full_auth)):
    token = (req.bot_token or "").strip()
    chat_id = (req.chat_id or "").strip()
    if token == "__unchanged__":
        # The Settings page never receives the stored token back (masked
        # endpoint above), so "save" with an untouched token field must not
        # blank a working credential -- the sentinel keeps it.
        existing = await auth.get_telegram_settings(app.state.auth_pool, user.id)
        if existing["configured"]:
            await auth.update_telegram_chat_only(app.state.auth_pool, user.id, chat_id or None)
            return await auth.get_telegram_settings(app.state.auth_pool, user.id)
        token = ""
    await auth.update_telegram(app.state.auth_pool, user.id, token or None, chat_id or None)
    return await auth.get_telegram_settings(app.state.auth_pool, user.id)


@app.post("/api/auth/me/telegram/test")
async def test_telegram_endpoint(user: User = Depends(require_full_auth)):
    """Sends a real message to THIS user's configured chat so the operator can
    verify the token/chat pair before trusting it with real alerts."""
    row = await auth.get_telegram_raw(app.state.auth_pool, user.id)
    if not row:
        raise HTTPException(400, "telegram is not configured -- save a bot token and chat id first")
    token, chat_id = row
    ok = await send_telegram(token, chat_id, task_alert(
        "done", "3d-agent", "Test alert from the dashboard settings page",
        0.00, "If you can read this, task alerts will reach you here."))
    if not ok:
        raise HTTPException(502, "telegram rejected the send -- check the bot token and chat id (and that you have messaged the bot once)")
    return {"ok": True}


@app.get("/api/auth/users")
async def list_users_endpoint(user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    rows = await auth.list_users(app.state.auth_pool)
    return [_user_public(auth._row_to_user(r)) for r in rows]


@app.post("/api/auth/users", status_code=201)
async def create_user_endpoint(req: CreateUserRequest, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "role must be 'admin' or 'user'")
    if req.allowed_repos:
        for r in req.allowed_repos:
            if r not in PROJECTS:
                raise HTTPException(400, f"unknown repo {r!r}")
    error = auth.validate_password_strength(req.password)
    if error:
        raise HTTPException(400, error)
    if await auth.get_user_by_email(app.state.auth_pool, req.email.strip().lower()):
        raise HTTPException(409, "a user with this email already exists")
    row = await auth.create_user(
        app.state.auth_pool, req.email.strip().lower(), req.password, req.role, req.allowed_repos,
        must_change_password=True,
    )
    if req.auto_approve_commands:
        await auth.update_auto_approve(app.state.auth_pool, row["id"], True)
        row = {**row, "auto_approve_commands": True}
    return _user_public(auth._row_to_user(row))


@app.patch("/api/auth/users/{user_id}")
async def update_user_access_endpoint(user_id: int, req: UpdateUserAccessRequest, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if req.allowed_repos:
        for r in req.allowed_repos:
            if r not in PROJECTS:
                raise HTTPException(400, f"unknown repo {r!r}")
    target = await auth.get_user_by_id(app.state.auth_pool, user_id)
    if not target:
        raise HTTPException(404, "user not found")
    # allowed_repos is meaningless for admin (always full access already);
    # auto_approve_commands is orthogonal to repo scope and applies to any
    # role, admin included -- it's the one most likely to want it.
    if req.allowed_repos is not None:
        if target["role"] == "admin":
            raise HTTPException(400, "the admin account always has full access")
        await auth.update_user_access(app.state.auth_pool, user_id, req.allowed_repos)
    if req.auto_approve_commands is not None:
        await auth.update_auto_approve(app.state.auth_pool, user_id, req.auto_approve_commands)
    return {"ok": True}


@app.delete("/api/auth/users/{user_id}")
async def delete_user_endpoint(user_id: int, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if user_id == user.id:
        raise HTTPException(400, "cannot delete your own account")
    target = await auth.get_user_by_id(app.state.auth_pool, user_id)
    if not target:
        raise HTTPException(404, "user not found")
    if target["role"] == "admin":
        raise HTTPException(400, "cannot delete the admin account")
    await auth.delete_user(app.state.auth_pool, user_id)
    return {"ok": True}


UPLOADS_DIRNAME = ".uploads"
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
# audit M-13: bound the number of files per upload and the absolute request
# body. Without these, `files: list[UploadFile]` was unbounded and a Content-
# Length ceiling existed nowhere (so JSON bodies were unbounded too).
UPLOAD_MAX_FILES = 20
REQUEST_BODY_MAX_BYTES = UPLOAD_MAX_FILES * UPLOAD_MAX_BYTES + 8 * 1024 * 1024
UPLOAD_KINDS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    ".pdf": "pdf",
    ".csv": "text", ".tsv": "text", ".txt": "text", ".json": "text", ".md": "text",
    ".xlsx": "sheet", ".xls": "sheet",
}

_GIT_EXCLUDES_PATH = Path(__file__).resolve().parent.parent / ".agent-git-excludes"


def _ensure_uploads_ignored(repo_root: str) -> None:
    """Uploads live inside the sandbox repo (so the agent's /workspace tools
    reach them) but must never enter a commit/review. A repo-external git
    excludes file (core.excludesFile) keeps them invisible to git without
    touching the project's own .gitignore -- zero diff, nothing for the
    reviewer to see."""
    import subprocess
    if not _GIT_EXCLUDES_PATH.exists():
        _GIT_EXCLUDES_PATH.write_text(f"{UPLOADS_DIRNAME}/\n")
    subprocess.run(["git", "-C", repo_root, "config", "core.excludesFile", str(_GIT_EXCLUDES_PATH)], check=False)


@app.post("/api/uploads")
async def upload_files(repo: str, files: list[UploadFile] = File(...), user: User = Depends(require_full_auth)):
    """Store operator attachments in the repo's sandbox under .uploads/<batch>/
    and return a manifest for the task goal. PDFs get a sibling .txt with the
    extracted text so the (text-only) coding models can read them directly;
    images are consumed via the agent's describe_image tool."""
    if repo not in PROJECTS:
        raise HTTPException(404, f"unknown repo {repo!r}")
    check_repo_access(user, repo)
    repo_root = PROJECTS[repo]["sandbox"]
    # audit M-34: _ensure_uploads_ignored is synchronous (Path.exists,
    # write_text, subprocess.run) -- run it off the event loop.
    await asyncio.to_thread(_ensure_uploads_ignored, repo_root)
    batch = uuid.uuid4().hex[:8]
    batch_dir = Path(repo_root) / UPLOADS_DIRNAME / batch
    batch_dir.mkdir(parents=True, exist_ok=True)

    # audit M-13: bound the file count before touching any of them.
    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(413, f"too many files ({len(files)}); limit is {UPLOAD_MAX_FILES}")

    manifest = []
    for f in files:
        name = Path(f.filename or "file").name  # strip any path components
        ext = Path(name).suffix.lower()
        kind = UPLOAD_KINDS.get(ext)
        if kind is None:
            raise HTTPException(415, f"unsupported file type {ext!r} ({name})")
        # audit M-13: stream to disk in chunks with a running counter, aborting
        # (and deleting the partial file) the moment it exceeds the cap -- the
        # old `await f.read()` materialized the whole file in memory first.
        dest = batch_dir / name
        written = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"{name} exceeds {UPLOAD_MAX_BYTES // (1024*1024)}MB")
                out.write(chunk)
        rel = f"{UPLOADS_DIRNAME}/{batch}/{name}"
        entry = {"path": rel, "kind": kind, "bytes": written}
        if kind == "pdf":
            # audit M-34: pypdf full-text extraction is CPU-bound for seconds on
            # a large PDF -- run it in a thread so it doesn't stall the loop.
            def _extract_pdf(dest_path: str, out_path: str) -> int:
                import pypdf
                reader = pypdf.PdfReader(dest_path)
                text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
                Path(out_path).write_text(text, encoding="utf-8")
                return len(reader.pages)
            try:
                pages = await asyncio.to_thread(_extract_pdf, str(dest), str(batch_dir / f"{name}.txt"))
                entry["extracted_text"] = f"{rel}.txt"
                entry["pages"] = pages
            except Exception as e:  # noqa: BLE001 -- a scanned/encrypted pdf shouldn't fail the upload
                entry["extracted_text"] = None
                entry["note"] = f"text extraction failed ({e}) -- possibly scanned; no text layer"
        manifest.append(entry)
    return {"repo": repo, "files": manifest}


def _attachments_note(attachments: list[dict]) -> str:
    """The goal suffix that tells the agent what was attached and how to
    consume each kind -- written for the model, not the human."""
    lines = ["", "", "--- ATTACHED FILES (operator-provided, in the repo workspace) ---"]
    for a in attachments:
        kind = a.get("kind")
        path = a.get("path", "(unknown path)")
        if kind == "image":
            lines.append(f"- {path} (image): view it with the describe_image tool -- ask it specific questions if needed.")
        elif kind == "pdf" and a.get("extracted_text"):
            lines.append(f"- {path} (PDF, {a.get('pages', '?')} pages): extracted text at {a.get('extracted_text')} -- read that with your read tool.")
        elif kind == "pdf":
            lines.append(f"- {path} (PDF): no text layer could be extracted ({a.get('note', '')}).")
        else:
            lines.append(f"- {path} ({kind}): read it directly with your read tool (or bash for large/structured files).")
    lines.append("These are reference inputs, not part of the codebase -- never commit them or copy them into the repo.")
    return "\n".join(lines)


class AttachmentEntry(BaseModel):
    # audit M-33: attachments were `list[dict]`, entirely unvalidated, and
    # _attachments_note indexed a['path'] unconditionally -- so {"kind":"image"}
    # (no path) was an unhandled KeyError -> 500, and the raw values landed in
    # the goal text the model reads. A real model rejects a malformed entry at
    # the API boundary with a 422 instead.
    kind: str
    path: str
    pages: int | None = None
    extracted_text: str | None = None
    note: str | None = None


class CreateTaskRequest(BaseModel):
    # audit M-33: goal was accepted empty/whitespace (send_planning_message
    # already rejected that -- the two endpoints were inconsistent), and
    # budget_usd had no bounds so 0 tripped the guard on the first call and a
    # negative value was accepted straight into AgentState.
    goal: str = Field(min_length=1, max_length=20_000)
    repo: str
    budget_usd: float | None = Field(default=None, gt=0, le=1000)
    attachments: list[AttachmentEntry] | None = None  # manifest entries from /api/uploads


class SendMessageRequest(BaseModel):
    text: str


class ResumeTaskRequest(BaseModel):
    additional_budget_usd: float
    message: str | None = None


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject", "respond"]
    message: str | None = None  # only meaningful for a reject -- explains why to the model


class SaveModelPinsRequest(BaseModel):
    pins: dict[str, str]  # {role: openrouter_model_id}


class CreatePlanningSessionRequest(BaseModel):
    repo: str


class PlanningMessageRequest(BaseModel):
    text: str
    attachments: list[dict] | None = None  # manifest entries from /api/uploads


_SUBSCRIBER_QUEUE_MAX = 2000


def _publish(task_id: str, event: dict) -> None:
    if event.get("execution_log"):
        _live_log_append(_live_task_log, task_id, event["execution_log"])
    for q, _ws in _subscribers.get(task_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # audit M-34: a reader this far behind is effectively gone; drop
            # rather than grow memory without bound. Its socket teardown will
            # remove it shortly.
            logger.warning("dropping event for a stalled task %s subscriber", task_id)


# The outer graph's own AgentState has no `plan`/`current_step_index` keys at
# all -- write_todos (deepagents' own planning tool, living in the inner
# deep-agent thread) is the plan, and `latest_todos` (a plain snapshot copied
# into the outer state at the end of each "work" pass, see work.py) is the
# closest equivalent. Translated here into the PlanStep[] shape the
# frontend's PlanTracker renders. `result`/`verified` have no todo-level
# equivalent in this design (verify_and_ship gates the whole task, not a
# per-step independently-checked claim) -- always False/None; the frontend
# doesn't currently render either field regardless.
_TODO_STATUS_MAP = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}


def _todos_to_plan(todos: list | None) -> list[dict] | None:
    if todos is None:
        return None
    return [
        {
            "id": str(i),
            "description": t.get("content", ""),
            "status": _TODO_STATUS_MAP.get(t.get("status"), "pending"),
            "result": None,
            "verified": False,
        }
        for i, t in enumerate(todos)
    ]


def _state_snapshot_for_frontend(values: dict) -> dict:
    """Used by get_task's REST snapshot (the hydrate path useTaskStream.ts
    calls on every connect/reconnect) -- without this translation, a page
    load/reconnect would show an empty plan until the next live "todos"
    custom event happened to arrive, since the raw checkpoint dict has
    `latest_todos`, not `plan`, and the frontend only reads the latter.
    """
    return {**values, "plan": _todos_to_plan(values.get("latest_todos")), "current_step_index": None}


def _apply_plan_fallback(snapshot: dict | None, meta_value: dict) -> dict | None:
    """Mid-pass, the checkpoint has no latest_todos yet (it is written only
    when a work pass RETURNS) -- fall back to the live mirror the todos
    handler keeps in the task meta, so the plan strip survives refreshes and
    task switches instead of vanishing until the pass ends (reported live
    2026-08-28). A checkpointed plan always wins over the mirror."""
    if snapshot is not None and snapshot.get("plan") is None and meta_value.get("latest_todos"):
        snapshot = {**snapshot, "plan": _todos_to_plan(meta_value.get("latest_todos"))}
    return snapshot


def _final_status(values: dict) -> str:
    """Terminal task status once a _stream_graph run's own astream loop
    ends -- "awaiting_approval" is a real third resting state alongside
    escalated/done (see deep_agent.py's INTERRUPT_ON, outer_graph.py's
    _route_after_verify), checked before the escalated/done fallback since
    a task can be both not-escalated and not-done: paused on a human-in-
    the-loop decision.
    """
    if values.get("escalated"):
        return "escalated"
    if values.get("pending_approval"):
        return "awaiting_approval"
    if values.get("pending_merge_approval"):
        # Review READY, merge parked on the operator's final look at the diff.
        return "awaiting_merge"
    return "done"


async def _stream_graph(task_id: str, repo: str, goal: str, budget_usd: float, graph_input, category: str | None = None) -> None:
    """Shared by a fresh task (graph_input = the initial state) and a resume
    (graph_input = None, meaning "continue from the last checkpoint" -- the
    standard LangGraph resume pattern). Everything after that point --
    streaming updates out over the WS, updating the Store, closing out
    status -- is identical either way.

    `category` is only ever passed explicitly on a fresh task (create_task
    classifies the goal once, up front). On a resume/approve call, it's left
    None here and recovered from the task's own already-stored meta below --
    a resume must never reclassify or lose the original category.
    """
    store = app.state.store
    graph = app.state.graph

    # audit H-19: read the stored meta ONCE up front and preserve the original
    # created_at across every terminal write below. Previously each write
    # hardcoded time.time(), so a resume reset the timestamp and list_tasks /
    # get_analytics (which sort and bucket by created_at) attributed a task to
    # its completion day instead of its start day.
    existing_meta = await store.aget(("tasks", repo), task_id)
    _existing_val = existing_meta.value if existing_meta else {}
    original_created_at = _existing_val.get("created_at")
    if category is None:
        category = _existing_val.get("category") or "other"
    # metadata/tags -- standard RunnableConfig fields LangChain's tracer
    # automatically attaches to every run generated within this invocation,
    # so a trace is filterable to this specific task in the LangSmith UI
    # rather than only to the project as a whole.
    thread_config = {
        "configurable": {"thread_id": task_id},
        "metadata": {"task_id": task_id, "repo": repo},
        "tags": [repo],
    }

    # cost_so_far here is Store-only display state -- hardcoding 0.0
    # unconditionally meant a resume briefly showed $0.00 in the
    # sidebar/stats for real work that already cost real money, until the
    # run finished and overwrote it with the true total. graph_input is
    # None specifically on a resume (see this function's own docstring), so
    # that's exactly when to carry the existing total forward instead. Note
    # the outer checkpoint's own cost_so_far is NOT always correct: it only
    # updates when a work pass fully returns, so a task cancelled mid-pass
    # has a stale value here until the CancelledError handler below patches
    # it back to the real total.
    starting_cost = 0.0
    if graph_input is None:
        existing = await graph.aget_state(thread_config)
        if existing and existing.values:
            starting_cost = existing.values.get("cost_so_far", 0.0)

    await store.aput(("tasks", repo), task_id, {
        "task_id": task_id, "goal": goal, "repo": repo, "budget_usd": budget_usd, "category": category,
        "status": "running", "created_at": original_created_at or time.time(), "cost_so_far": starting_cost,
    })
    _publish(task_id, {"type": "status", "status": "running"})

    # Declared here (not just inside the loop below) so the CancelledError
    # handler can always read the latest live-tracked cost, even if
    # cancellation lands before the astream loop yields anything.
    last_meta_cost = starting_cost

    try:
        async with project_lock(repo):
            # stream_mode=["updates", "custom"] (not just "updates") -- the
            # graph's own "work" node is a single StateGraph node that
            # manually drives a whole inner deep-agent run inside itself
            # (see work.py), so a plain "updates" stream would yield exactly
            # one event for the entire work pass, arriving only once it's
            # fully done. work_node's own get_stream_writer() calls emit
            # "custom" events (todos/log_entry) as each inner model turn or
            # tool call actually happens, including subagent-delegated ones
            # (tagged "work:<subagent_name>") -- these are what provide real
            # live streaming here.
            #
            # durability="sync" is the right tradeoff for this outer graph
            # specifically: "async" (the library default) checkpoints in the
            # background while the next step runs, which means a crash
            # between a step completing and its checkpoint write landing
            # could lose that last step. work<->verify_and_ship transitions
            # happen at most every few seconds to minutes (bounded by real
            # LLM/subprocess work, not by this), so the extra confirm-write
            # latency is negligible -- unlike the deep agent's own inner
            # astream_events() call (work.py), which stays on the library
            # default deliberately, since that graph's steps are much
            # higher-frequency (many rapid LLM/tool turns per single outer
            # "work" pass) and durability there isn't independently
            # controllable per-node anyway.
            async for mode, payload in graph.astream(
                graph_input, thread_config, stream_mode=["updates", "custom"], durability="sync"
            ):
                if mode == "custom":
                    if payload.get("type") == "todos":
                        _publish(task_id, {
                            "type": "node_update",
                            "node": "work",
                            "plan": _todos_to_plan(payload.get("todos")),
                        })
                        # Mirror into the Store meta, same pattern (and same
                        # reason) as the live cost mirror below: the outer
                        # checkpoint's latest_todos is only written when a
                        # work pass RETURNS -- a pass can run 30+ minutes, and
                        # until then the plan existed only as this ephemeral
                        # event. A refresh or task-switch mid-pass rebuilt
                        # from the checkpoint and the step strip came back
                        # empty (reported live 2026-08-28). Display state
                        # only; nothing enforcement-related reads it.
                        try:
                            _meta = await store.aget(("tasks", repo), task_id)
                            if _meta:
                                await store.aput(("tasks", repo), task_id,
                                                 {**_meta.value, "latest_todos": payload.get("todos")})
                        except Exception:  # noqa: BLE001 -- a display mirror must never break the stream
                            logger.exception("todos mirror write failed for %s", task_id)
                    elif payload.get("type") == "log_entry":
                        entry = payload["entry"]
                        _publish(task_id, {
                            "type": "node_update",
                            "node": entry["node"],
                            "execution_log": [entry],
                        })
                    elif payload.get("type") == "cost":
                        # Live mid-pass cost from work.py's tracker (see
                        # _consume_values there) -- without this, cost only
                        # updates on outer node boundaries, and a single work
                        # pass can run well past ten minutes showing $0.00 the
                        # whole time. Mirrored into the Store meta too,
                        # throttled to >= $0.02 moves, so the tasks sidebar
                        # (which reads meta, not the stream) tracks as well.
                        # Display-only either way -- budget enforcement reads
                        # the tracker/checkpoint, never this.
                        live_cost = payload["cost_so_far"]
                        _publish(task_id, {
                            "type": "node_update",
                            "node": "work",
                            "cost_so_far": live_cost,
                        })
                        if live_cost - last_meta_cost >= 0.005:
                            meta_item = await store.aget(("tasks", repo), task_id)
                            if meta_item:
                                last_meta_cost = live_cost
                                await store.aput(("tasks", repo), task_id, {**meta_item.value, "cost_so_far": live_cost})
                    continue

                # mode == "updates": one event per outer node ("work" or
                # "verify_and_ship") once its whole pass completes -- carries
                # the fields custom events don't (cost_so_far, escalated,
                # review_gate_result). Only forward keys the node's own
                # return dict actually included (not update.get(key, default))
                # -- verify_and_ship.py's own loop_back/no-diff/escalated-guard
                # paths each return a deliberately sparse dict (e.g. a
                # checks-failed loop-back never touches "escalated" at all),
                # and defaulting a missing key to False/None here would ship
                # a stale/wrong value over the wire instead of just omitting
                # the field (which the frontend already treats as "unchanged"
                # via its own `?? previousValue` merge in useTaskStream.ts).
                for node_name, update in payload.items():
                    if not isinstance(update, dict):
                        continue
                    node_payload: dict = {"type": "node_update", "node": node_name}
                    for key in (
                        "execution_log", "cost_so_far", "escalated", "escalation_reason",
                        "review_gate_result", "pending_approval", "committed_sha",
                    ):
                        if key in update:
                            node_payload[key] = update[key]
                    if "latest_todos" in update:
                        node_payload["plan"] = _todos_to_plan(update["latest_todos"])
                    _publish(task_id, node_payload)
                    # Keep the Store meta's display cost current per pass, not
                    # just at terminal transitions -- otherwise the tasks list
                    # (which reads meta, unlike the task view's
                    # checkpoint-backed hydrate) can show cost frozen at
                    # whatever the last terminal write recorded during a long
                    # multi-round run, wrongly suggesting the budget tracker
                    # had stalled. Enforcement never reads this -- the ceiling
                    # checks the checkpoint's own cost_so_far -- this is
                    # purely so the visible number tracks reality.
                    if "cost_so_far" in update:
                        last_meta_cost = update["cost_so_far"]
                        meta_item = await store.aget(("tasks", repo), task_id)
                        if meta_item:
                            await store.aput(("tasks", repo), task_id, {**meta_item.value, "cost_so_far": update["cost_so_far"]})

        final = await graph.aget_state(thread_config)
        values = final.values
        status = _final_status(values)
        await store.aput(("tasks", repo), task_id, {
            "task_id": task_id, "goal": goal, "repo": repo, "budget_usd": values.get("budget_usd", budget_usd),
            "category": category, "status": status, "created_at": original_created_at or time.time(), "cost_so_far": values.get("cost_so_far", 0.0),
            "escalation_reason": values.get("escalation_reason"),
        })
        _publish(task_id, {
            "type": "status",
            "status": status,
            "escalation_reason": values.get("escalation_reason"),
            "pending_approval": values.get("pending_approval"),
        })
        # Telegram: every rest state IS the actionable moment -- escalated,
        # waiting on an approval, waiting on the merge look, or done.
        _detail = None
        if status == "escalated":
            _detail = values.get("escalation_reason")
        elif status == "awaiting_approval":
            _pa = values.get("pending_approval") or {}
            _detail = _pa.get("description") if isinstance(_pa, dict) else str(_pa)
        elif status == "awaiting_merge":
            _pm = values.get("pending_merge_approval") or {}
            _sha = str(_pm.get("sha", ""))[:12] if isinstance(_pm, dict) else ""
            _detail = f"commit {_sha} passed review -- approve the merge in the dashboard"
        _alert_task_status(task_id, status, repo, goal, values.get("cost_so_far"), _detail)
    except asyncio.CancelledError:
        # The operator's Stop button (/stop below) cancels this task
        # directly. CancelledError is a BaseException, not an Exception, so
        # it never reaches the broad handler below -- without this branch a
        # stopped task's Store status would stay stuck on "running" forever,
        # identical-looking to a genuinely orphaned task with no way to tell
        # the two apart.
        #
        # cost_now uses last_meta_cost, not a fresh checkpoint read: the
        # outer checkpoint's cost_so_far only updates when a work pass fully
        # returns, so cancelling mid-pass (the common case -- Stop almost
        # always interrupts an actively-running pass) leaves it stale at
        # whatever it was before this pass started. last_meta_cost is kept
        # current throughout the pass (both from work.py's live per-call
        # "cost" events and from each node's own completed-pass update), so
        # it reflects real spend right up to the moment of cancellation.
        # Also patched back into the checkpoint itself (not just the Store
        # meta) so a future resume's BudgetTracker starts counting from the
        # real total instead of silently under-billing against the budget
        # ceiling.
        cost_now = last_meta_cost
        # audit M-34: every write in this handler is best-effort. A raising or
        # slow store.aput must NOT replace the CancelledError -- doing so 500'd a
        # Stop that actually worked and left the UI unsure whether it took. We
        # always re-raise CancelledError at the end regardless of these writes.
        try:
            await graph.aupdate_state(thread_config, {"cost_so_far": cost_now})
        except Exception:
            pass
        try:
            await store.aput(("tasks", repo), task_id, {
                "task_id": task_id, "goal": goal, "repo": repo, "budget_usd": budget_usd, "category": category,
                "status": "stopped", "created_at": original_created_at or time.time(), "cost_so_far": cost_now,
            })
            _publish(task_id, {"type": "status", "status": "stopped"})
        except Exception:  # noqa: BLE001
            logger.exception("stopped-status write failed for task %s (cancellation still honored)", task_id)
        raise
    except Exception as e:  # noqa: BLE001 -- deliberately broad: any failure here must still flip status away from "running"
        # str(e) alone is close to useless for some exception types -- a bare
        # KeyError's str() is just the missing key's repr with zero context
        # on where it was raised. Full traceback to the log; the Store/UI
        # still only need the short message, that's what the user sees.
        logger.error("task %s failed: %s", task_id, str(e))
        logger.error(traceback.format_exc())
        await store.aput(("tasks", repo), task_id, {
            "task_id": task_id, "goal": goal, "repo": repo, "budget_usd": budget_usd, "category": category,
            # audit H-20: carry cost_so_far and escalation_reason forward. This
            # was a full overwrite with neither key, so a failed task erased the
            # spend already mirrored mid-pass and read as $0.00 in get_stats /
            # get_analytics. last_meta_cost holds the latest live-tracked total.
            "status": "error", "created_at": original_created_at or time.time(), "error": str(e),
            "cost_so_far": last_meta_cost,
            "escalation_reason": _existing_val.get("escalation_reason"),
        })
        _publish(task_id, {"type": "status", "status": "error", "error": str(e)})
        _alert_task_status(task_id, "error", repo, goal, last_meta_cost, str(e)[:400])
    finally:
        _publish(task_id, {"type": "closed"})
        _running_tasks.pop(task_id, None)


async def _run_task(
    task_id: str, goal: str, repo: str, budget_usd: float, category: str,
    auto_approve_commands: bool = False,
    require_merge_review: bool = True,
) -> None:
    state = initial_state(
        task_id=task_id, goal=goal, repo=repo, budget_usd=budget_usd,
        auto_approve_commands=auto_approve_commands,
        require_merge_review=require_merge_review,
    )
    await _stream_graph(task_id, repo, goal, budget_usd, state, category=category)


async def _read_with_retry(fn):
    """One retry for the read-only store/checkpointer lookups the frontend
    polls constantly (task list, stats, analytics, single-task fetch).

    The connection pool (agent/graph.py's open_checkpointer/open_store) is
    the actual fix for the class of failure this guards against: it
    validates a connection's health at checkout
    (`check=AsyncConnectionPool.check_connection`) before handing it to any
    caller, which is what a Postgres restart used to break silently -- with
    a single long-lived raw connection and no reconnect logic, every request
    touching it would 500 until the process was restarted.

    This retry is defense in depth on top of that, not a replacement for it:
    it covers the residual window where a connection dies after the pool's
    own checkout check but before/during the call itself (a real race, just
    a narrow one). Deliberately scoped to read-only calls only -- retrying a
    write here would mean thinking hard about idempotency per call site, and
    the writes in _stream_graph/_run_task (the live task-execution path)
    don't need it: they go through the exact same pool and get the same
    checkout validation for free.
    """
    try:
        return await fn()
    except PgOperationalError:
        await asyncio.sleep(0.25)
        return await fn()


async def _resolve_task_repo(task_id: str) -> str | None:
    """A handful of task endpoints (message/stop) only ever needed task_id
    before per-user repo access existed -- this looks up which repo a task
    belongs to from its own checkpoint state, the same source resume/
    approve/get/delete already read `repo` from directly."""
    checkpoint = await app.state.graph.aget_state({"configurable": {"thread_id": task_id}})
    if not checkpoint or not checkpoint.values:
        return None
    return checkpoint.values.get("repo")


@app.get("/api/repos")
def list_repos(user: User = Depends(require_full_auth)):
    return [r for r in PROJECTS if user.can_access(r)]


# ---------------------------------------------------------------------------
# Planning chat -- a conversational research/design-consulting session
# (agent/planning_chat.py), distinct from a build task: no plan/execute/
# verify graph, no budget ceiling, no write/edit/bash access to the repo.
# Its own Store namespace ("planning", repo) holds lightweight session meta
# (repo, created_at, updated_at, title, plan_markdown); the full
# conversation itself lives in the same Postgres checkpointer tasks use,
# under thread_id f"planning:{session_id}" (see planning_thread_config).
# "Build Now" in the frontend does NOT call anything here -- it just calls
# the existing POST /api/tasks with the saved plan_markdown as the goal,
# reusing the real build system entirely as-is.
# ---------------------------------------------------------------------------


# Live-log buffers (2026-08-28): the detailed stream entries (chat bubbles,
# tool chips) previously existed ONLY as in-flight WS events -- the durable
# sources hold much less (a task's checkpoint keeps per-pass summaries; a
# planning thread's messages get REWRITTEN by summarization), so a refresh or
# task switch mid-run swapped a rich live view for a skeleton. Each publisher
# now also appends its log entries here, and the hydrate endpoints return
# whichever source is fuller. In-process by design: it makes refresh/switch
# lossless while the server lives, costs no store churn, and after a backend
# restart the durable sources are still the fallback they always were.
_LIVE_LOG_MAX_ENTRIES = 3000   # matches the frontend's MAX_LOG_ENTRIES cap
_LIVE_LOG_MAX_KEYS = 12        # LRU-ish: enough for every concurrently-viewed run
_live_task_log: dict[str, list] = {}
_live_planning_log: dict[str, list] = {}


def _live_log_append(book: dict, key: str, entries: list) -> None:
    buf = book.get(key)
    if buf is None:
        while len(book) >= _LIVE_LOG_MAX_KEYS:
            book.pop(next(iter(book)))
        buf = book[key] = []
    buf.extend(entries)
    if len(buf) > _LIVE_LOG_MAX_ENTRIES:
        del buf[: len(buf) - _LIVE_LOG_MAX_ENTRIES]


def _fuller_log(buffered: list | None, durable: list | None) -> list:
    """The hydrate rule: live buffer wins only by being LONGER -- a durable
    source that caught up (or a fresh process with an empty buffer) is never
    shadowed by a stale one."""
    b, d = buffered or [], durable or []
    return b if len(b) > len(d) else d


# Last Telegram-alerted (status, detail) per task, in-process: a resumed task
# re-enters _stream_graph and re-derives the same rest state; the operator
# needs ONE phone buzz per distinct stop, not one per stream cycle.
_last_task_alert: dict[str, tuple] = {}


def _notify_bg(text: str) -> None:
    """The one door alerts leave through: resolves the auth pool defensively
    so an alert can NEVER break the code path it decorates -- app.state has
    no auth_pool during unit tests and the earliest startup moments, and
    accessing a missing State attribute raises."""
    pool = getattr(app.state, "auth_pool", None)
    if pool is None:
        return
    notify_operators_bg(pool, text)


def _alert_task_status(task_id: str, status: str, repo: str, goal: str, cost: float | None, detail: str | None) -> None:
    """Telegram alert for a task's rest state -- deduped, best-effort."""
    if status in ("running", "stopped"):
        # running is noise; stopped is the operator's own Stop button --
        # alerting someone about the button they just pressed is spam.
        return
    key = (status, str(detail or "")[:120])
    if _last_task_alert.get(task_id) == key:
        return
    _last_task_alert[task_id] = key
    _notify_bg(task_alert(status, repo, goal, cost, detail))


def _publish_planning(session_id: str, event: dict) -> None:
    if event.get("type") == "log_entry" and event.get("entry"):
        _live_log_append(_live_planning_log, session_id, [event["entry"]])
    for q, _ws in _planning_subscribers.get(session_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("dropping event for a stalled planning %s subscriber", session_id)  # audit M-34


@app.post("/api/planning/sessions", status_code=201)
async def create_planning_session(req: CreatePlanningSessionRequest, user: User = Depends(require_full_auth)):
    if req.repo not in PROJECTS:
        raise HTTPException(400, f"unknown repo {req.repo!r}, must be one of {list(PROJECTS)}")
    check_repo_access(user, req.repo)
    session_id = uuid.uuid4().hex
    await app.state.store.aput(("planning", req.repo), session_id, {
        "session_id": session_id, "repo": req.repo, "created_at": time.time(),
        "updated_at": time.time(), "title": None, "plan_markdown": None, "cost_usd": 0.0,
        "archived": False, "category": None,
    })
    return {"session_id": session_id, "repo": req.repo}


@app.get("/api/planning/sessions")
async def list_planning_sessions(repo: str | None = None, user: User = Depends(require_full_auth)):
    store = app.state.store
    if repo:
        check_repo_access(user, repo)
        repos = [repo]
    else:
        repos = [r for r in PROJECTS if user.can_access(r)]
    items = []
    for r in repos:
        results = await _read_with_retry(lambda r=r: store.asearch(("planning", r), limit=100))
        items.extend(item.value for item in results)
    items.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return items


async def _find_planning_meta(session_id: str):
    """Session meta is stored per-repo (("planning", repo)), but the id
    routes here carry no repo -- cheap enough to check the handful of
    configured repos rather than also threading repo through every URL."""
    for repo in PROJECTS:
        item = await app.state.store.aget(("planning", repo), session_id)
        if item:
            return repo, item.value
    return None, None


@app.get("/api/planning/sessions/{session_id}")
async def get_planning_session(session_id: str, user: User = Depends(require_full_auth)):
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    agent, _plan_ref, _tracker = await build_planning_agent(
        config, repo, app.state.checkpointer, app.state.store, starting_cost=meta.get("cost_usd", 0.0)
    )
    thread_config = planning_thread_config(session_id, repo)
    checkpoint = await agent.aget_state(thread_config)
    messages = (checkpoint.values.get("messages") or []) if checkpoint and checkpoint.values else []
    log = [e for e in (_translate_planning_message(m) for m in messages) if e]
    # The checkpoint is a LOSSY source for planning: summarization rewrites
    # the message list, discarding the old entries wholesale. The live buffer
    # holds the full scrollback while the server lives.
    log = _fuller_log(_live_planning_log.get(session_id), log)
    return {"meta": meta, "log": log, "running": session_id in _running_planning_turns}


@app.post("/api/planning/sessions/{session_id}/archive")
async def archive_planning_session(session_id: str, user: User = Depends(require_full_auth)):
    """Closes out a planning conversation without deleting it -- its full
    history/plan stays reachable (same as an "archived" task), it just drops
    out of the sidebar's default active list. Hit from the "New Plan"
    button once the operator is done with the current plan, whether or not
    they actually built from it."""
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    await app.state.store.aput(("planning", repo), session_id, {**meta, "archived": True})
    return {"ok": True}


async def _bank_planning_turn(session_id: str, repo: str, plan_markdown: str | None, spent: float | None, text: str | None = None) -> None:
    """Persist whatever a turn earned before it ended -- used by the two
    abnormal exits (operator Stop, and an exception), which both used to
    throw work away that the session had genuinely already paid for.

    A crashed turn still HAS a plan if the model called save_plan before the
    crash: run_planning_turn returns plan_ref["markdown"], and a crash means
    it never returned, so that draft lives nowhere else. The old error path
    didn't look at it, so the operator saw a red error AND lost the plan the
    model had just written. The cancel path had the same hole.

    Same PRESERVE-never-clobber rule as the success path: a None plan leaves
    the stored one alone (a turn can add or replace a plan, never remove
    one), and cost is banked because the spend is real either way.

    `text` seeds a missing title the same way the success path does. Titles
    used to be set only on success, so a session whose FIRST turn failed sat
    in the sidebar as a permanent None -- both timed-out sessions on
    2026-08-27 did exactly that. No classify_task call here (the success path
    does one for the category): teardown after a failure is the wrong moment
    for another model call, and a title alone is what the sidebar needs.
    """
    try:
        item = await app.state.store.aget(("planning", repo), session_id)
        if not item:
            return
        meta = {**item.value, "updated_at": time.time(), "turn_active": False}
        if plan_markdown is not None:
            meta["plan_markdown"] = plan_markdown
        if spent is not None:
            meta["cost_usd"] = spent
        if text and not meta.get("title"):
            meta["title"] = text[:60]
        await app.state.store.aput(("planning", repo), session_id, meta)
    except Exception:  # noqa: BLE001 -- teardown must never raise over the failure it is cleaning up after
        logger.exception("failed to persist planning progress for session %s", session_id)


async def _run_planning_turn_bg(session_id: str, repo: str, text: str, attachments: list[dict] | None = None, allowed_repos: list[str] | None = None) -> None:
    try:
        meta_item = await app.state.store.aget(("planning", repo), session_id)
        starting_cost = meta_item.value.get("cost_usd", 0.0) if meta_item else 0.0
        # Classified fresh per turn -- a conversation can drift from easy
        # design chat into a real bug report mid-session, and the model
        # should follow that. Classified on the clean, operator-typed text
        # -- not the attachments note appended below, same reasoning as
        # create_task's own goal/attachments split.
        #
        # STICKY UPWARD (2026-08-28): escalation is per-turn, de-escalation
        # never happens within a session. A HARD session's continuation
        # nudges are short by nature ("continue", "also check X") and
        # classify EASY on their text alone -- which flipped a session's
        # model mid-plan: half a strata plan was written by the HARD pin and
        # half by the EASY pin after a nudge (operator report; a restart
        # exposed it, but any short follow-up triggers the same flip). Once
        # a session has needed the hard model, its context IS the hard
        # problem -- every later turn reasons over that same context, so the
        # floor ratchets up and stays. A fresh session starts the ladder
        # over.
        difficulty = await classify_planning_difficulty(text, config)
        _prior_difficulty = (meta_item.value.get("difficulty") if meta_item else None) or "EASY"
        if _prior_difficulty == "HARD":
            difficulty = "HARD"
        # turn_active/turn_started_at make an in-flight planning turn
        # STORE-VISIBLE, like a running task. Deploy tooling used to infer
        # idleness from tasks + router quiet, and a >90s gap inside one long
        # model call read as idle -- a restart landed mid-plan (the incident
        # that also exposed the difficulty flip above). Cleared on every
        # ending path; the timestamp lets a reader ignore a marker staler
        # than the 1800s turn ceiling (a hard-killed process can't clear it).
        if meta_item:
            await app.state.store.aput(("planning", repo), session_id, {
                **meta_item.value, "difficulty": difficulty,
                "turn_active": True, "turn_started_at": time.time(),
            })
        # Seed the turn with the draft this session already has. The agent (and
        # its plan_ref) is rebuilt per turn, so without this the model cannot see
        # its own previous plan and every turn starts from a blank one.
        _prior = await app.state.store.aget(("planning", repo), session_id)
        _prior_plan = _prior.value.get("plan_markdown") if _prior else None
        agent, plan_ref, tracker = await build_planning_agent(
            config, repo, app.state.checkpointer, app.state.store,
            starting_cost=starting_cost, difficulty=difficulty,
            existing_plan=_prior_plan,
            allowed_repos=allowed_repos,  # audit H-2
        )
        thread_config = planning_thread_config(session_id, repo)
        # Circuit breaker: llm_for_role's own per-call timeout (plus
        # ChatOpenAI's default retries) can still leave a turn hanging for
        # 10+ minutes with zero log_entry published and no exception ever
        # raised if the underlying call genuinely never returns (confirmed
        # live 2026-08-23 -- a planning turn sat with no checkpoint, no log
        # line beyond entering astream_events, and no error surfaced for
        # over 10 minutes). Without this, that reads to the operator as "the
        # agent is stuck" with nothing to even look at. agent-planning-chat's
        # own per-call timeout is 450s (reasoning_effort="high" genuinely
        # needs that long -- see llm_for_role), and a real turn can involve
        # several tool-calling round trips, so this outer ceiling has to
        # clear several such calls comfortably; it exists purely so the
        # except Exception below always fires eventually instead of never.
        message_text = text + _attachments_note(attachments) if attachments else text
        _live_log_append(_live_planning_log, session_id, [{
            "kind": "user", "summary": text[:200], "detail": text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])
        plan_markdown = await asyncio.wait_for(
            run_planning_turn(
                agent, plan_ref, thread_config, message_text,
                # cost events arrive pre-shaped ({"type": "cost", ...}); log
                # entries need wrapping -- route on the shape.
                lambda ev: _publish_planning(
                    session_id,
                    ev if isinstance(ev, dict) and ev.get("type") == "cost"
                    else {"type": "log_entry", "entry": ev},
                ),
                tracker=tracker,
            ),
            timeout=1800,
        )
        # PRESERVE, never clobber. `plan_markdown` is only what THIS turn's
        # save_plan call produced, and the system prompt deliberately does not
        # ask the model to re-save every turn ("call this once the plan is
        # genuinely ready, and again any time it meaningfully changes").
        # Writing it unconditionally meant any later turn that merely discussed
        # something erased a plan the session had already earned -- the operator
        # sees several good rounds and then no plan to send to Build, with no
        # error anywhere. A turn can add or replace a plan, never remove one.
        effective_plan = plan_markdown if plan_markdown is not None else _prior_plan
        meta_item = await app.state.store.aget(("planning", repo), session_id)
        if meta_item:
            meta = {
                **meta_item.value, "updated_at": time.time(),
                "plan_markdown": effective_plan, "cost_usd": tracker.total_cost,
                "turn_active": False,
            }
            if not meta.get("title"):
                meta["title"] = text[:60]
                # Classified once, on the first real message -- same fixed
                # taxonomy/classifier as a build task (agent/classify.py),
                # reused as-is rather than a separate planning-specific one,
                # so the sidebar can group planning sessions by category the
                # same way it already groups tasks (see Sidebar.tsx) instead
                # of one flat, ever-growing list under a single "Planning"
                # bucket.
                classification = await classify_task(text, config)
                meta["category"] = classification.category
            await app.state.store.aput(("planning", repo), session_id, meta)
        _publish_planning(session_id, {"type": "turn_complete", "plan_markdown": effective_plan, "cost_usd": tracker.total_cost})
    except asyncio.CancelledError:
        # Operator pressed Stop, or the process is shutting down. The spend is
        # real either way, so bank it against the session rather than losing it,
        # and keep whatever plan the session already had -- a stopped turn must
        # never be the thing that erases a plan.
        logger.info("planning turn cancelled for session %s", session_id)
        # `tracker` is created partway through the try, so a cancel that lands
        # early leaves it unbound -- fall back to the cost the session already
        # had rather than raising NameError out of a cancellation handler.
        _t = locals().get("tracker")
        spent = _t.total_cost if _t is not None else locals().get("starting_cost", 0.0)
        # `plan_ref` is bound partway through the try, same as `tracker` -- a
        # cancel landing before build_planning_agent leaves both unbound.
        _ref = locals().get("plan_ref") or {}
        await _bank_planning_turn(session_id, repo, _ref.get("markdown"), spent, text=text)
        _publish_planning(session_id, {"type": "stopped", "cost_usd": spent})
        raise
    except Exception as e:  # noqa: BLE001 -- must surface to the client, never die silently in the background
        logger.exception("planning turn failed for session %s", session_id)
        _t_alert = locals().get("tracker")
        _notify_bg(task_alert(
            "planning_error", repo, text, _t_alert.total_cost if _t_alert is not None else None, str(e)[:400]))
        # The spend up to the failure is just as real as a cancelled turn's,
        # and this path banked NEITHER it nor the draft -- a turn that crashed
        # after save_plan lost the plan and under-reported the session's cost.
        _t = locals().get("tracker")
        _ref = locals().get("plan_ref") or {}
        await _bank_planning_turn(
            session_id, repo, _ref.get("markdown"),
            _t.total_cost if _t is not None else None,
            text=text,
        )
        _publish_planning(session_id, {"type": "error", "message": str(e)})
    finally:
        _publish_planning(session_id, {"type": "closed"})
        _running_planning_turns.pop(session_id, None)


@app.post("/api/planning/sessions/{session_id}/stop")
async def stop_planning_turn(session_id: str, user: User = Depends(require_full_auth)):
    """Cancels the in-flight planning turn and waits for teardown before
    responding -- same contract as stop_task, and for the same reason: firing
    cancel() and returning immediately would report "stopped" to the UI while a
    web_search or a 450s reasoning call is still in flight.

    A planning turn can legitimately run for many minutes (agent-planning-chat
    runs at reasoning_effort=high), so without this the only way to end one was
    to wait it out or restart the service -- and restarting kills the turn with
    no record, leaving the UI waiting on a turn that no longer exists.

    Fails CLOSED on repo resolution, same as stop_task: an authorization check
    that no-ops when it cannot reach its input is not a check.
    """
    repo, _meta = await _find_planning_meta(session_id)
    if not repo:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    turn = _running_planning_turns.get(session_id)
    if not turn:
        raise HTTPException(409, "planning session is not processing a message")
    turn.cancel()
    try:
        await turn
    except asyncio.CancelledError:
        pass
    return {"ok": True}


@app.post("/api/planning/sessions/{session_id}/message", status_code=202)
async def send_planning_message(session_id: str, req: PlanningMessageRequest, user: User = Depends(require_full_auth)):
    if session_id in _running_planning_turns:
        raise HTTPException(409, "planning session is already processing a message")
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    if not req.text.strip():
        raise HTTPException(400, "message text is required")
    _running_planning_turns[session_id] = asyncio.create_task(
        _run_planning_turn_bg(session_id, repo, req.text.strip(), req.attachments, allowed_repos=user.allowed_repos)
    )
    return {"ok": True}


@app.websocket("/api/planning/sessions/{session_id}/stream")
async def stream_planning_session(ws: WebSocket, session_id: str):
    # WebSocket.cookies is populated from the handshake's headers before
    # accept() is ever called -- validate first and close outright (never
    # accept then immediately drop) for an unauthorized or repo-mismatched
    # connection attempt.
    user = await auth.get_user_from_ws_cookie(app.state.auth_pool, ws.cookies)
    if not user:
        await ws.close(code=4401)
        return
    # audit H-1: enforce the same forced-screen gates as require_full_auth. A
    # valid session cookie alone must not open the live stream while the user
    # is parked behind the forced password-change / 2FA-setup screen.
    if _forced_screen_block(user):
        await ws.close(code=4403)
        return
    repo, meta = await _find_planning_meta(session_id)
    if not meta or not user.can_access(repo):
        await ws.close(code=4403)
        return
    await ws.accept()
    # No eviction of prior connections -- see stream_task's own comment on
    # this exact same pattern. Multiple viewers on the same planning session
    # (different users, or the same user in two tabs) all stay live
    # simultaneously; each connection's own receiver() below cleans up only
    # its own entry when it actually disconnects.
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)  # audit M-34
    _planning_subscribers.setdefault(session_id, []).append((queue, ws))

    async def sender():
        while True:
            # Heartbeat (2026-08-28): a long model call can mean 60s+ of
            # total socket silence, and NAT/middleboxes between the operator
            # and this VPS kill idle TCP without telling either end -- the
            # browser's stream just goes still until a manual refresh
            # (reported live). A ping every 20s keeps bytes flowing through
            # every hop, and when the socket IS dead, send_json raises here
            # promptly so the client gets a real close event to react to
            # instead of silence. Clients ignore the "ping" type.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(event)
            if event.get("type") == "closed":
                break

    async def receiver():
        while True:
            await ws.receive()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.exception()
        for task in pending:
            task.cancel()
    finally:
        entry = (queue, ws)
        if entry in _planning_subscribers.get(session_id, []):
            _planning_subscribers[session_id].remove(entry)
        if session_id in _planning_subscribers and not _planning_subscribers[session_id]:
            del _planning_subscribers[session_id]  # audit M-34


@app.post("/api/tasks", status_code=201)
async def create_task(req: CreateTaskRequest, user: User = Depends(require_full_auth)):
    if req.repo not in PROJECTS:
        raise HTTPException(400, f"unknown repo {req.repo!r}, must be one of {list(PROJECTS)}")
    check_repo_access(user, req.repo)
    # audit M-33: reject a whitespace-only goal (Field min_length=1 still lets a
    # lone space through), matching send_planning_message's own check.
    goal = req.goal.strip()
    if not goal:
        raise HTTPException(422, "goal must not be empty")
    task_id = str(uuid.uuid4())
    budget = req.budget_usd or config.default_budget_usd
    # Classified on the clean, operator-typed goal -- not the attachments
    # note appended below, which is boilerplate for the model, not signal
    # about what kind of task this is.
    # audit M-34: bound how long task creation blocks on the classifier. It
    # already self-times-out at 15s, but the operator shouldn't wait that long
    # for a task id over a label that's mostly for Analytics -- fall back to the
    # neutral classification if it's slow.
    try:
        classification = await asyncio.wait_for(classify_task(goal, config), timeout=8)
    except asyncio.TimeoutError:
        logger.warning("task classification exceeded 8s; starting with fallback classification")
        classification = TaskClassification(category="other", needs_tests=False)
    if req.attachments:
        goal = goal + _attachments_note([a.model_dump() for a in req.attachments])
    if classification.needs_tests:
        # An explicit directive, not a hope: the coordinator's system prompt
        # already tells it to delegate test-writing "when the task calls for
        # it", but nothing enforces that judgment call -- this makes the
        # judgment call for it up front, for the one signal (new/changed
        # testable logic) classify_task can actually assess before any code
        # has been read.
        goal = goal + TEST_REMINDER_NOTE
    _running_tasks[task_id] = asyncio.create_task(
        _run_task(
            task_id, goal, req.repo, budget, classification.category,
            # Snapshot of the creator's own settings -- see outer_state.py.
            auto_approve_commands=user.auto_approve_commands,
            require_merge_review=user.require_merge_review,
        )
    )
    return {"task_id": task_id, "category": classification.category, "needs_tests": classification.needs_tests}


@app.get("/api/tasks")
async def list_tasks(repo: str | None = None, user: User = Depends(require_full_auth)):
    store = app.state.store
    if repo:
        check_repo_access(user, repo)
        repos = [repo]
    else:
        repos = [r for r in PROJECTS if user.can_access(r)]
    items = []
    for r in repos:
        results = await _read_with_retry(lambda r=r: store.asearch(("tasks", r), limit=50))
        items.extend(item.value for item in results)
    items.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return items


# The commit-reviewer service's own dashboard API (router credit balance,
# per-model spend). The frontend used to call that service directly, but a
# deployment may put it behind a separate reverse-proxy auth that this app's
# own users have no session for (this one did). That silently 401'd the
# balance fetch for anyone who had only logged into 3D-Agent's own auth,
# and BalanceStrip.tsx swallows any fetch failure (renders nothing rather
# than an error), so the balance just vanished from the sidebar with no
# visible cause. This passthrough re-uses this app's own auth instead, so
# the balance only ever depends on being logged into 3D-Agent itself.
_REVIEW_SERVICE_BASE_URL = "http://127.0.0.1:4100"


@app.get("/api/router-balance")
async def get_router_balance(user: User = Depends(require_full_auth)):
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_REVIEW_SERVICE_BASE_URL}/api/router/balance")
        resp.raise_for_status()
        return resp.json()


@app.get("/api/stats")
async def get_stats(user: User = Depends(require_full_auth)):
    """Per-repo cost/outcome aggregates, computed fresh from the Store on
    every call rather than maintained as a running counter -- this backend
    doesn't run often enough or handle enough volume for that to matter, and
    computing fresh avoids a counter silently drifting from reality.
    """
    auth.require_admin(user)
    store = app.state.store
    per_repo = {}
    total_cost = 0.0
    total_tasks = 0
    status_counts = {"running": 0, "done": 0, "escalated": 0, "error": 0}

    for repo in PROJECTS:
        results = await _read_with_retry(lambda repo=repo: store.asearch(("tasks", repo), limit=200))
        tasks = [item.value for item in results]
        repo_cost = sum(t.get("cost_so_far", 0.0) or 0.0 for t in tasks)
        per_repo[repo] = {
            "task_count": len(tasks),
            "total_cost": repo_cost,
            "status_counts": {
                s: sum(1 for t in tasks if t.get("status") == s) for s in ("running", "done", "escalated", "error")
            },
        }
        total_cost += repo_cost
        total_tasks += len(tasks)
        for t in tasks:
            s = t.get("status")
            if s in status_counts:
                status_counts[s] += 1

    return {
        "per_repo": per_repo,
        "total_cost": total_cost,
        "total_tasks": total_tasks,
        "status_counts": status_counts,
    }


@app.get("/api/analytics")
async def get_analytics(user: User = Depends(require_full_auth)):
    """Chart-ready aggregates for the Analytics view, computed fresh from the
    Store on every call (same freshness-over-counters reasoning as /api/stats,
    which stays as-is for the lighter sidebar/balance uses). Sources:

    - ("tasks", repo) meta entries: per-task cost/status/created_at/budget --
      drives the daily-spend series, outcome donut, and per-task cost bars.
    - ("episodes", repo) records (verify_and_ship writes one per terminal
      outcome): iteration counts and review verdicts -- genuine depth the task
      meta alone doesn't carry.
    """
    auth.require_admin(user)
    import json as _json
    from datetime import datetime, timezone

    store = app.state.store

    tasks: list[dict] = []
    episodes: list[dict] = []
    for repo in PROJECTS:
        results = await _read_with_retry(lambda repo=repo: store.asearch(("tasks", repo), limit=200))
        for item in results:
            tasks.append({**item.value, "repo": repo})
        ep_results = await _read_with_retry(lambda repo=repo: store.asearch(("episodes", repo), limit=200))
        for item in ep_results:
            value = item.value
            # Episodes are stored as backend file entries ({"content": json-str})
            content = value.get("content") if isinstance(value, dict) else None
            if isinstance(content, str):
                try:
                    record = _json.loads(content)
                    record["repo"] = repo
                    episodes.append(record)
                except (ValueError, TypeError):
                    pass

    tasks.sort(key=lambda t: t.get("created_at", 0))

    # Daily spend series, zero-filled over the last 14 days so a young
    # deployment renders as a real chart, not one floating point on an
    # empty grid. Spend is attributed to the day it actually happened using
    # episode records: each episode carries the task's cumulative cost at
    # that terminal moment, so per-day spend for a task is the delta
    # between consecutive episodes -- accurate for tasks that span days,
    # unlike bucketing everything on the creation date. Tasks with no
    # episodes yet (still running, or pre-episode history) fall back to
    # their full current cost on their creation day.
    from datetime import timedelta

    daily: dict[str, dict] = {}
    today = datetime.now(tz=timezone.utc).date()
    for offset in range(13, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        daily[day] = {"date": day, "cost": 0.0, "tasks": 0}

    def _bucket(day: str):
        # Days older than the window still aggregate into its oldest day so
        # totals stay honest rather than silently dropping history.
        return daily[day] if day in daily else daily[min(daily)]

    episodes_by_task: dict[str, list[dict]] = {}
    for e in sorted(episodes, key=lambda e: e.get("timestamp") or ""):
        if e.get("task_id") and e.get("timestamp") and e.get("cost_usd") is not None:
            episodes_by_task.setdefault(e["task_id"], []).append(e)

    for task_episodes in episodes_by_task.values():
        # Running max, not last-seen: a task's episode costs are cumulative
        # but not perfectly monotonic (a node retry can resume from a
        # checkpoint whose cost predates a crashed attempt's spend), and
        # naive clamped deltas over-count on every dip. Deltas against the
        # running max telescope to exactly the task's peak cumulative cost
        # regardless of dips.
        running_max = 0.0
        for e in task_episodes:
            day = str(e["timestamp"])[:10]
            cost = float(e["cost_usd"])
            delta = max(0.0, cost - running_max)
            running_max = max(running_max, cost)
            _bucket(day)["cost"] += delta

    for t in tasks:
        ts = t.get("created_at")
        if not ts:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        _bucket(day)["tasks"] += 1
        if t.get("task_id") not in episodes_by_task:
            _bucket(day)["cost"] += float(t.get("cost_so_far") or 0.0)

    daily_series = [daily[k] for k in sorted(daily)]

    per_task = [
        {
            "task_id": t.get("task_id"),
            "repo": t.get("repo"),
            "goal": (t.get("goal") or "")[:80],
            "category": t.get("category") or "other",
            "cost": float(t.get("cost_so_far") or 0.0),
            "budget": float(t.get("budget_usd") or 0.0),
            "status": t.get("status"),
            "created_at": t.get("created_at"),
        }
        for t in tasks
    ]

    # Cost/count grouped by the classifier's fixed taxonomy (agent/classify.py)
    # instead of one bar per individual task -- a per-task chart stops being
    # readable once there are more than a handful of tasks, and doesn't answer
    # the question that actually matters: which KIND of work is costing the
    # most. Tasks created before classification existed have no "category"
    # key at all (not even "other"), so they're grouped there via the same
    # fallback the frontend already treats as the default bucket.
    by_category: dict[str, dict] = {}
    for t in tasks:
        cat = t.get("category") or "other"
        bucket = by_category.setdefault(cat, {"category": cat, "tasks": 0, "cost": 0.0})
        bucket["tasks"] += 1
        bucket["cost"] += float(t.get("cost_so_far") or 0.0)
    by_category_list = sorted(by_category.values(), key=lambda b: b["cost"], reverse=True)

    outcomes: dict[str, int] = {}
    for t in tasks:
        s = t.get("status") or "unknown"
        outcomes[s] = outcomes.get(s, 0) + 1

    per_repo = {}
    for repo in PROJECTS:
        repo_tasks = [t for t in tasks if t.get("repo") == repo]
        per_repo[repo] = {
            "tasks": len(repo_tasks),
            "cost": sum(float(t.get("cost_so_far") or 0.0) for t in repo_tasks),
        }

    iteration_stats = [
        {
            "task_id": e.get("task_id"),
            "repo": e.get("repo"),
            "iterations": e.get("iteration_count"),
            "outcome": e.get("outcome"),
            "cost": e.get("cost_usd"),
            "review_verdict": e.get("review_verdict"),
            "timestamp": e.get("timestamp"),
        }
        for e in episodes
        if e.get("iteration_count") is not None
    ]

    # audit M-34: _reviewer_usage reads an ever-growing JSONL whole -- off-loop.
    reviewer_usage = await asyncio.to_thread(_reviewer_usage)

    return {
        "daily": daily_series,
        "per_task": per_task,
        "by_category": by_category_list,
        "outcomes": outcomes,
        "per_repo": per_repo,
        "episodes": iteration_stats,
        # Episode-derived (the daily series' own sum), not the surviving task
        # metas' sum -- deleting a task removes its meta but its spend still
        # happened, and the two totals disagreeing on the dashboard would be
        # a visible inconsistency.
        "total_cost": sum(b["cost"] for b in daily_series),
        "total_tasks": len(tasks),
        # The reviewer's spend, which this view could not previously see at all.
        # Kept as its own block rather than folded into total_cost: the agent's
        # spend and the gate's spend are different budgets and mixing them would
        # silently change what every existing number on this page means.
        "reviewer": reviewer_usage,
    }


# ── Reviewer spend ───────────────────────────────────────────────────────────
REVIEWER_USAGE_LOG = paths.REVIEWER_USAGE_LOG


def _reviewer_usage() -> dict:
    """Aggregate the commit reviewer's own model spend.

    The reviewer is a separate service that, until 2026-08-25, called OpenRouter
    directly with a hardcoded model and never read the response's `usage` block.
    So its spend existed but appeared nowhere: Analytics is built from the
    agent's own task/episode records and structurally could not see it. That
    made "cost per task" wrong in a specific way — it excluded the review rounds
    a task actually needed, which is precisely the cost a NEEDS_FIXES loop adds.

    Returns zeros rather than raising when the log is absent (a fresh install,
    or the reviewer simply not having run yet).
    """
    out = {
        "reviews": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0,
        "per_repo": [], "daily": [], "model": None, "cost_known": True,
    }
    try:
        lines = REVIEWER_USAGE_LOG.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        # Genuinely no spend yet (fresh install / reviewer never ran) -- a true
        # zero, so cost_known stays True.
        return out
    except Exception as e:  # noqa: BLE001
        # audit M-34: a file that exists but could not be READ is NOT a known
        # zero -- returning cost_known:True here was the exact silent-zero this
        # function's docstring says it exists to eliminate. Flag it unknown.
        logger.warning("reviewer usage log unreadable: %s", e)
        out["cost_known"] = False
        return out

    by_repo: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    missing_cost = 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        out["reviews"] += 1
        cost = r.get("cost")
        if cost is None:
            missing_cost += 1
        else:
            out["cost"] += float(cost)
        ti, to = int(r.get("prompt_tokens") or 0), int(r.get("completion_tokens") or 0)
        out["tokens_in"] += ti
        out["tokens_out"] += to
        out["model"] = r.get("model") or out["model"]

        repo = r.get("project") or "unknown"
        b = by_repo.setdefault(repo, {"repo": repo, "reviews": 0, "cost": 0.0})
        b["reviews"] += 1
        b["cost"] += float(cost or 0.0)

        day = str(r.get("at") or "")[:10]
        if day:
            d = by_day.setdefault(day, {"date": day, "cost": 0.0, "reviews": 0})
            d["cost"] += float(cost or 0.0)
            d["reviews"] += 1

    # Say so rather than quietly under-reporting: a run whose usage lacked a cost
    # field contributes tokens but not dollars.
    out["cost_known"] = missing_cost == 0
    out["reviews_missing_cost"] = missing_cost
    out["per_repo"] = sorted(by_repo.values(), key=lambda x: -x["cost"])
    out["daily"] = sorted(by_day.values(), key=lambda x: x["date"])
    return out


# audit M-32: asyncio keeps only a WEAK reference to a bare create_task, so a
# fire-and-forget background refresh could be garbage-collected mid-run and
# silently never happen. Hold a strong reference until the task finishes, and
# log any exception it raised (bare create_task also swallows those).
_background_tasks: set = set()


def _spawn_background(coro, label: str) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("background task %s failed: %r", label, t.exception())

    task.add_done_callback(_done)


_model_usage_cache: dict = {"at": 0.0, "data": None}
_model_usage_lock = asyncio.Lock()
_MODEL_USAGE_TTL_S = 600
_MODEL_USAGE_WINDOW_DAYS = 14
_MODEL_USAGE_MAX_RUNS = 800

_tool_reliability_cache: dict = {"at": 0.0, "data": None}
_tool_reliability_lock = asyncio.Lock()
_TOOL_RELIABILITY_TTL_S = 600
_TOOL_RELIABILITY_WINDOW_DAYS = 14
_TOOL_RELIABILITY_MAX_RUNS = 3000

_trace_summary_cache: dict = {"at": 0.0, "data": None}
_trace_summary_lock = asyncio.Lock()
_TRACE_SUMMARY_TTL_S = 600
_TRACE_SUMMARY_WINDOW_DAYS = 14
_TRACE_SUMMARY_MAX_RUNS = 3000


# The model-usage section deliberately never looks earlier than when the
# per-role model pins went live, so a prior era's routing history doesn't
# pollute the current per-role breakdown.
from datetime import datetime as _dt, timezone as _tz
_MODEL_PINNING_CUTOVER = _dt(2026, 8, 20, 12, 45, tzinfo=_tz.utc)


def _classify_model_usage_role(metadata: dict, alias: str | None) -> str:
    """Which agent role made this call, for the Analytics model-usage
    breakdown.

    The background consolidation agent (agent/consolidation.py) runs on its
    own thread_id scheme ("consolidation:{repo}:{timestamp}") and carries
    none of the tags below, so without checking for it explicitly its calls
    silently fell into the coordinator bucket -- and since its model
    ("reasoning-tier") never matches the agent-planner alias, they landed
    specifically in "coder", making it look like the coder role used
    multiple different models when those calls were unrelated background
    traffic. Checked first, before any of the live-task tags below.

    agent/classify.py's one-shot classification call is the same class of
    bug in a different shape: it's a bare ChatOpenAI.ainvoke() outside any
    graph, so it carries no thread_id/lc_agent_name/lc_source at all --
    caught here by alias instead, since there's no thread_id prefix to key
    off like consolidation has.
    """
    thread_id = metadata.get("thread_id") or ""
    if thread_id.startswith("consolidation:"):
        return "consolidation"
    if alias == "agent-classifier":
        return "classifier"
    if alias == "agent-vision":
        # describe_image is callable from the coordinator and from several
        # subagents (investigator/test-writer/general-purpose), so without
        # this check a vision call would inherit whichever lc_agent_name (or
        # lack of one) belongs to its caller and get folded into that role's
        # bucket instead of standing on its own. Checked by alias, same
        # reasoning as agent-classifier above.
        return "vision"
    if alias == "agent-planning-chat":
        # Checked by alias, not thread_id ("planning:{session_id}") -- the
        # planning agent also runs its own SummarizationMiddleware pinned to
        # agent-summarizer, and that call's alias won't match this branch,
        # so it correctly falls through to the lc_source check below instead
        # of getting folded into "planning-chat" the way a thread_id-first
        # check would misattribute it.
        return "planning-chat"
    # deepagents tags every subagent's runs with lc_agent_name
    # (investigator/test-writer/general-purpose); SummarizationMiddleware's
    # own calls carry lc_source=summarization; everything else is the
    # coordinator itself.
    # ANY pinned alias names its role outright -- the general form of the
    # classifier/vision special-cases above. Before this, a traced call whose
    # alias was agent-cartographer (or any future role) fell through to the
    # coordinator branch and landed in the CODER bucket: the analytics panel
    # showed mistral/haiku/gemini rows under Coder that were really
    # cartographer runs, consolidator probes and benchmarks -- "models that
    # are not in the stack", as the operator put it.
    if isinstance(alias, str) and alias.startswith("agent-"):
        role = alias.removeprefix("agent-")
        if role == "planning-chat-hard":
            role = "planning-chat"   # one bucket for both planning tiers
        return role

    role = metadata.get("lc_agent_name") or (
        "summarizer" if metadata.get("lc_source") == "summarization" else None
    )
    if role:
        return role
    # No agent tag and not a pinned alias: this is BACKGROUND traffic (direct
    # library calls, benchmarks, one-off scripts). It used to be silently
    # filed under coder via the coordinator fallback, doubling that bucket
    # with models no role ever pinned. Quarantine it instead.
    return "background"


def _scan_langsmith_model_usage() -> list[dict]:
    """Aggregates this agent's real per-model usage from LangSmith traces.

    Scoped by construction to the current (deepagents-based) system: tracing
    was enabled the day this system shipped, so nothing from a prior
    iteration can appear here. The real underlying model per call comes from
    response_metadata.model_name (the router's return_raw_model_name), not
    the router alias. Sync client -> runs via asyncio.to_thread.
    """
    from datetime import datetime, timedelta, timezone

    import langsmith as ls

    # audit M-6: don't touch LangSmith when tracing is off. The help text
    # promises "no trace data leaves this machine at all"; the old code built a
    # bare ls.Client() and 401'd on every boot and 600s refresh.
    if not config.langsmith_tracing:
        return []

    client = ls.Client()
    project = __import__("os").environ.get("LANGSMITH_PROJECT", "3d-agent")
    # Never scan earlier than the model-pinning cutover: an earlier
    # pool/classifier era used many different coordinator models, which
    # aren't relevant to the current pinned-role breakdown this section shows.
    start = max(
        datetime.now(tz=timezone.utc) - timedelta(days=_MODEL_USAGE_WINDOW_DAYS),
        _MODEL_PINNING_CUTOVER,
    )
    usage: dict[str, dict] = {}
    scanned = 0
    for run in client.list_runs(project_name=project, run_type="llm", start_time=start):
        scanned += 1
        if scanned > _MODEL_USAGE_MAX_RUNS:
            break
        model = None
        tokens_in = tokens_out = 0
        try:
            generations = (run.outputs or {}).get("generations") or []
            if generations and generations[0]:
                kwargs = (generations[0][0].get("message") or {}).get("kwargs") or {}
                response_metadata = kwargs.get("response_metadata") or {}
                model = response_metadata.get("model_name") or response_metadata.get("model")
                # Pinned-role aliases (agent-coder, agent-planner, ...) echo
                # the alias, not the underlying model (return_raw_model_name
                # only works for auto_router deployments). The alias is also
                # the robust role key -- classify coordinator turns by it
                # before resolving, so swapping which model backs a role
                # can never mislabel history scanned across the swap boundary.
                alias = model
                model = resolve_alias(model)
                # Canonicalize before aggregation keys on it: the same model
                # arrives as "openrouter/x/y" from some paths and "x/y" from
                # others, which rendered as two visually identical rows under
                # one role -- the "doubled reporting" the operator flagged.
                if isinstance(model, str) and model.startswith("openrouter/"):
                    model = model.removeprefix("openrouter/")
                usage_metadata = kwargs.get("usage_metadata") or {}
                tokens_in = int(usage_metadata.get("input_tokens") or 0)
                tokens_out = int(usage_metadata.get("output_tokens") or 0)
        except (AttributeError, TypeError, IndexError):
            pass
        if not model:
            continue
        metadata = (run.extra or {}).get("metadata", {}) if run.extra else {}
        role = _classify_model_usage_role(metadata, alias)
        bucket = usage.setdefault((role, model), {
            "role": role, "model": model, "calls": 0, "tokens_in": 0, "tokens_out": 0, "duration_total_s": 0.0, "duration_count": 0,
        })
        bucket["calls"] += 1
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out
        if run.start_time and run.end_time:
            bucket["duration_total_s"] += (run.end_time - run.start_time).total_seconds()
            bucket["duration_count"] += 1
    result = []
    for u in usage.values():
        avg_latency_s = (u["duration_total_s"] / u["duration_count"]) if u["duration_count"] else None
        result.append({
            "role": u["role"], "model": u["model"], "calls": u["calls"],
            "tokens_in": u["tokens_in"], "tokens_out": u["tokens_out"], "avg_latency_s": avg_latency_s,
        })
    return sorted(result, key=lambda u: u["calls"], reverse=True)


async def _refresh_model_usage() -> None:
    """A real asyncio.Lock, not a bare boolean flag -- a concurrent caller
    (e.g. the very first page load after a restart, racing the lifespan's
    own pre-warm task) actually waits for the in-flight scan and then reuses
    its result, rather than seeing "a refresh is already running" and
    returning immediately with the cache still empty. The old boolean-flag
    version did exactly that: get_model_usage's own cold-cache branch awaits
    this function expecting real data back, but if the pre-warm task had
    already flipped the flag, this call was a silent no-op and the endpoint
    fell through to an empty result instead of blocking -- confirmed live,
    not hypothetical (the same pattern reproduced this exact way on
    /api/analytics/trace-summary right after a restart)."""
    async with _model_usage_lock:
        if _model_usage_cache["data"] is not None and time.time() - _model_usage_cache["at"] < _MODEL_USAGE_TTL_S:
            return  # someone else refreshed it while we were waiting for the lock
        try:
            data = await asyncio.to_thread(_scan_langsmith_model_usage)
            _model_usage_cache["data"] = data
            _model_usage_cache["at"] = time.time()
        except Exception as e:  # noqa: BLE001 -- LangSmith being down must never break the page
            logger.warning("model usage scan failed: %s", e)


@app.get("/api/analytics/models")
async def get_model_usage(user: User = Depends(require_full_auth)):
    """Serve-stale-while-revalidate -- the LangSmith scan can exceed nginx's
    proxy timeout when run inline, and the in-memory cache dies with every
    pm2 restart. A page load right after a restart would otherwise pay the
    full cold scan, time out, and the whole model-usage section would
    silently vanish from the dashboard. Now: any cached data (even expired)
    returns instantly with a background refresh kicked off; only the very
    first request after a cold start ever blocks, and startup pre-warming
    (see lifespan) makes even that rare.
    """
    auth.require_admin(user)
    now = time.time()
    data = _model_usage_cache["data"]
    if data is not None:
        if now - _model_usage_cache["at"] >= _MODEL_USAGE_TTL_S:
            _spawn_background(_refresh_model_usage(), "refresh_model_usage")
        return {"models": data, "cached": True, "tracing_disabled": not config.langsmith_tracing}
    await _refresh_model_usage()
    return {"models": _model_usage_cache["data"] or [], "cached": False, "tracing_disabled": not config.langsmith_tracing}


def _scan_langsmith_tool_reliability() -> dict:
    """Aggregates real tool-call error rates from LangSmith traces: per-tool
    totals/errors for the reliability bar chart, plus a daily error-count
    series so a spike (e.g. the binary-file-dump incident, or a genuinely
    flaky tool) is visible over time rather than buried in a single lifetime
    percentage. Sync client -> runs via asyncio.to_thread, same as the
    model-usage scan.

    Excludes the background consolidation agent's own tool calls (thread_id
    starting "consolidation:") -- same reasoning as
    _classify_model_usage_role: that's unrelated background traffic, not
    live task execution, and would dilute the real per-tool error rate.
    """
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    import langsmith as ls

    # audit M-6: skip when tracing is off (see the model-usage scan).
    if not config.langsmith_tracing:
        return {"tools": [], "daily": []}

    client = ls.Client()
    project = __import__("os").environ.get("LANGSMITH_PROJECT", "3d-agent")
    start = datetime.now(tz=timezone.utc) - timedelta(days=_TOOL_RELIABILITY_WINDOW_DAYS)

    by_tool: dict[str, dict] = {}
    daily_errors: dict[str, int] = defaultdict(int)
    scanned = 0
    for run in client.list_runs(project_name=project, run_type="tool", start_time=start):
        scanned += 1
        if scanned > _TOOL_RELIABILITY_MAX_RUNS:
            break
        metadata = (run.extra or {}).get("metadata", {}) if run.extra else {}
        thread_id = metadata.get("thread_id") or ""
        if thread_id.startswith("consolidation:"):
            continue
        name = run.name or "unknown"
        bucket = by_tool.setdefault(name, {"tool": name, "calls": 0, "errors": 0})
        bucket["calls"] += 1
        is_error = bool(run.error)
        if is_error:
            bucket["errors"] += 1
            if run.start_time:
                daily_errors[run.start_time.strftime("%Y-%m-%d")] += 1

    tools = sorted(
        [{**t, "error_rate": (t["errors"] / t["calls"]) if t["calls"] else 0.0} for t in by_tool.values()],
        key=lambda t: t["errors"], reverse=True,
    )

    # Zero-filled daily series over the same window, matching /api/analytics'
    # own daily-spend series convention -- a quiet day is a real, informative
    # zero, not a gap.
    today = datetime.now(tz=timezone.utc).date()
    daily = [
        {"date": (today - timedelta(days=offset)).strftime("%Y-%m-%d"),
         "errors": daily_errors.get((today - timedelta(days=offset)).strftime("%Y-%m-%d"), 0)}
        for offset in range(_TOOL_RELIABILITY_WINDOW_DAYS - 1, -1, -1)
    ]

    return {"tools": tools, "daily": daily}


async def _refresh_tool_reliability() -> None:
    """Real lock, not a bare flag -- see _refresh_model_usage's own
    docstring for why."""
    async with _tool_reliability_lock:
        if _tool_reliability_cache["data"] is not None and time.time() - _tool_reliability_cache["at"] < _TOOL_RELIABILITY_TTL_S:
            return
        try:
            data = await asyncio.to_thread(_scan_langsmith_tool_reliability)
            _tool_reliability_cache["data"] = data
            _tool_reliability_cache["at"] = time.time()
        except Exception as e:  # noqa: BLE001 -- LangSmith being down must never break the page
            logger.warning("tool reliability scan failed: %s", e)


@app.get("/api/analytics/tool-reliability")
async def get_tool_reliability(user: User = Depends(require_full_auth)):
    """Same serve-stale-while-revalidate contract as /api/analytics/models
    -- see that endpoint's own docstring."""
    auth.require_admin(user)
    now = time.time()
    data = _tool_reliability_cache["data"]
    if data is not None:
        if now - _tool_reliability_cache["at"] >= _TOOL_RELIABILITY_TTL_S:
            _spawn_background(_refresh_tool_reliability(), "refresh_tool_reliability")
        return {**data, "cached": True, "tracing_disabled": not config.langsmith_tracing}
    await _refresh_tool_reliability()
    return {**(_tool_reliability_cache["data"] or {"tools": [], "daily": []}), "cached": False, "tracing_disabled": not config.langsmith_tracing}


def _scan_langsmith_trace_summary() -> dict:
    """Top-level trace health: how many full traces ran, how long they took
    end-to-end, and what fraction errored -- distinct from the per-llm-call
    scan above (_scan_langsmith_model_usage) and the per-tool-call scan
    above that (_scan_langsmith_tool_reliability). A "trace" here is a root
    run (is_root=True): one per work/verify pass, planning turn, or
    subagent invocation started fresh -- not every individual llm/tool call
    inside it.

    Token totals are summed from the model-usage cache instead of walking
    LLM runs a second time here -- same underlying runs, no need to re-scan
    them just to get a different aggregate.
    """
    from datetime import datetime, timedelta, timezone

    import langsmith as ls

    # audit M-6: skip when tracing is off (see the model-usage scan).
    if not config.langsmith_tracing:
        return {"trace_count": 0, "avg_latency_s": None, "error_rate": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0}

    client = ls.Client()
    project = __import__("os").environ.get("LANGSMITH_PROJECT", "3d-agent")
    start = datetime.now(tz=timezone.utc) - timedelta(days=_TRACE_SUMMARY_WINDOW_DAYS)

    trace_count = 0
    error_count = 0
    duration_total_s = 0.0
    duration_count = 0
    scanned = 0
    for run in client.list_runs(project_name=project, is_root=True, start_time=start):
        scanned += 1
        if scanned > _TRACE_SUMMARY_MAX_RUNS:
            break
        trace_count += 1
        if run.error:
            error_count += 1
        if run.start_time and run.end_time:
            duration_total_s += (run.end_time - run.start_time).total_seconds()
            duration_count += 1

    model_usage = _model_usage_cache.get("data") or []
    return {
        "trace_count": trace_count,
        "avg_latency_s": (duration_total_s / duration_count) if duration_count else None,
        "error_rate": (error_count / trace_count) if trace_count else 0.0,
        "total_input_tokens": sum(u["tokens_in"] for u in model_usage),
        "total_output_tokens": sum(u["tokens_out"] for u in model_usage),
    }


async def _refresh_trace_summary() -> None:
    """Real lock, not a bare flag -- see _refresh_model_usage's own
    docstring for why."""
    async with _trace_summary_lock:
        if _trace_summary_cache["data"] is not None and time.time() - _trace_summary_cache["at"] < _TRACE_SUMMARY_TTL_S:
            return
        try:
            data = await asyncio.to_thread(_scan_langsmith_trace_summary)
            _trace_summary_cache["data"] = data
            _trace_summary_cache["at"] = time.time()
        except Exception as e:  # noqa: BLE001 -- LangSmith being down must never break the page
            logger.warning("trace summary scan failed: %s", e)


@app.get("/api/analytics/trace-summary")
async def get_trace_summary(user: User = Depends(require_full_auth)):
    """Same serve-stale-while-revalidate contract as /api/analytics/models
    -- see that endpoint's own docstring."""
    auth.require_admin(user)
    now = time.time()
    data = _trace_summary_cache["data"]
    if data is not None:
        if now - _trace_summary_cache["at"] >= _TRACE_SUMMARY_TTL_S:
            _spawn_background(_refresh_trace_summary(), "refresh_trace_summary")
        return {**data, "cached": True, "tracing_disabled": not config.langsmith_tracing}
    await _refresh_trace_summary()
    empty = {"trace_count": 0, "avg_latency_s": None, "error_rate": 0.0, "total_input_tokens": 0, "total_output_tokens": 0}
    return {**(_trace_summary_cache["data"] or empty), "cached": False, "tracing_disabled": not config.langsmith_tracing}


@app.post("/api/tasks/{task_id}/message")
async def send_message(task_id: str, req: SendMessageRequest, user: User = Depends(require_full_auth)):
    if task_id not in _running_tasks:
        raise HTTPException(409, "task is not running -- nothing would read this message")
    repo = await _resolve_task_repo(task_id)
    # Fail CLOSED. This used to be `if repo:` -- so a task whose repo could
    # not be resolved (checkpoint not yet written, a transient store read
    # failure) skipped the access check entirely and any authenticated user
    # could act on it. An authorization check that silently no-ops when it
    # can't reach its input is not a check.
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    add_message(task_id, req.text)
    # Published immediately so the UI shows the nudge landed right away --
    # work_node (agent/nodes/work.py) drains this mailbox at the start of
    # its next pass, which can be much later than the moment this message
    # was sent (a "work" pass can span many inner turns before
    # verify_and_ship ever loops back) -- a message that visibly "goes
    # nowhere" in the meantime is worse than no feedback.
    _publish(task_id, {
        "type": "node_update",
        "node": "operator",
        "execution_log": [{
            "node": "operator",
            "step_id": None,
            "summary": "message sent -- will be picked up at the start of the next work pass",
            "detail": req.text,
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    })
    return {"ok": True}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, user: User = Depends(require_full_auth)):
    """Cancels the asyncio task actually driving this run and waits for it
    to finish tearing down before responding -- firing task.cancel() and
    returning immediately would tell the UI "stopped" while a shell command
    or LLM call might still be mid-flight for another few seconds, which is
    exactly the "doesn't actually stop" gap this exists to close. run_shell
    (agent/tools/shell.py) kills the whole process group on cancellation
    too, not just abandons the await -- a naive stop here would otherwise
    leave e.g. a typecheck/test subprocess and everything it forked running
    orphaned on the server. _stream_graph's own CancelledError handler
    records the real cost-so-far and flips Store status to "stopped" before
    this returns.
    """
    repo = await _resolve_task_repo(task_id)
    # Fail CLOSED. This used to be `if repo:` -- so a task whose repo could
    # not be resolved (checkpoint not yet written, a transient store read
    # failure) skipped the access check entirely and any authenticated user
    # could act on it. An authorization check that silently no-ops when it
    # can't reach its input is not a check.
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    task = _running_tasks.get(task_id)
    if not task:
        raise HTTPException(409, "task is not running")
    task.cancel()
    # audit M-34: bound the wait for teardown. A slow Postgres in the
    # CancelledError handler could otherwise hang this request indefinitely; the
    # cancellation itself has already been requested, so after the timeout we
    # return and let teardown finish in the background rather than block the UI.
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.warning("stop_task: teardown for %s still running after 30s; returning anyway", task_id)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, req: ResumeTaskRequest, user: User = Depends(require_full_auth)):
    """Escalation is a circuit breaker, not a dead end -- the graph's full
    state (plan, execution log, cost so far) is still sitting in the
    checkpointer, so an escalated or budget-exhausted task has a path back
    in: add budget and continue rather than being stuck looking abandoned.

    Also covers a task orphaned by a backend restart mid-run -- Store still
    says "running" but nothing is actually driving it (this pm2 process gets
    restarted routinely to deploy fixes; a task that happened to be running
    at that moment would otherwise look "running" forever with no way back
    in either).

    These two cases need different handling:

    - Orphaned/stopped: patch `budget_usd`/`max_iterations`/`task_id`
      without `as_node` -- LangGraph resumes at whatever node the checkpoint
      already has as `next` (either "work", if the process died mid-work-pass
      or before one ever started, or "verify_and_ship", if it died between a
      completed work pass and the gate re-running). Nothing about escalated/
      pending_feedback is touched, so routing is exactly whatever it already
      was -- no replanning, no lost progress.
    - Genuine escalation: `_route_after_verify` (outer_graph.py) checks
      `state["escalated"]` first, before pending_feedback, so simply
      patching `escalated=False` is not enough on its own -- it also needs a
      truthy `pending_feedback` to route to "work" rather than falling
      through to END. Patched `as_node="verify_and_ship"` so the conditional
      edge re-evaluates against the new state and correctly resolves to
      "work". `pending_feedback` here doubles as the actual instruction the
      deep agent sees as a new HumanMessage on its next work.py pass (case 2
      in work.py's own docstring) -- not just a routing signal, real
      content: what the escalation was and that budget was added, plus the
      operator's own resume message if they left one.
    """
    if task_id in _running_tasks:
        raise HTTPException(409, "task is already running")
    graph = app.state.graph
    thread_config = {"configurable": {"thread_id": task_id}}
    checkpoint = await graph.aget_state(thread_config)
    if not checkpoint or not checkpoint.values:
        raise HTTPException(404, "task not found")
    values = checkpoint.values
    check_repo_access(user, values["repo"])

    meta = await app.state.store.aget(("tasks", values["repo"]), task_id)
    store_status = meta.value.get("status") if meta else None
    was_escalated = bool(values.get("escalated"))
    # "done" is resumable too, not fully terminal like any other completion:
    # the "two consecutive no-diff passes -> done, no changes needed"
    # safeguard in verify_and_ship.py (built to stop genuinely-finished
    # investigations from looping forever) can't always tell a real
    # conclusion apart from the model dropping a tool call mid-investigation.
    # An operator who judges a "done" verdict premature needs a way back in,
    # the same as an escalation -- there's no substitute for a human catching
    # a wrong "done" and saying "no, keep going."
    was_done = (not was_escalated) and store_status == "done"
    # "running" here means orphaned (Store says running, nothing actually
    # driving it) rather than genuinely running, since the endpoint itself
    # already 409s above when task_id is truly in _running_tasks.
    # "stopped" is the operator's own Stop button (/stop below) -- same
    # "nothing lost, just paused" situation as an orphaned task, so it's
    # resumable through the exact same non-replanning path.
    # audit H-20: "error" is resumable too. verify_and_ship's catch-all exists
    # specifically to route around an unresumable error status, but anything
    # raising outside that handler still lands with an intact checkpoint the
    # endpoint used to refuse -- contradicting "a task is never a dead end."
    # The checkpoint is intact, so a resume continues from the last good state.
    resumable = was_escalated or was_done or store_status in ("running", "stopped", "error")
    if not resumable:
        raise HTTPException(409, f"task status is {store_status!r} -- nothing to resume")
    if was_done and not req.message:
        # Unlike an escalation (which always has a real reason to restate),
        # a "done" task has nothing to nudge with on its own -- silently
        # reopening it with no instruction would just re-run the same
        # investigation and likely land on the same premature conclusion.
        raise HTTPException(400, "resuming a done task requires a message telling it what to do next")

    new_budget = values["budget_usd"] + req.additional_budget_usd
    # max_iterations is set once at task creation (40) and, unlike
    # budget_usd, was never bumped on resume -- every work<->verify_and_ship
    # cycle across the task's entire lifetime counts against that same fixed
    # cap, no matter how many times it's legitimately been resumed (backend
    # restarts, manual stop/resume, operator nudges). Without growing it on
    # each resume, a task resumed several times over one long session could
    # hit iteration_count == max_iterations with cost_so_far nowhere near
    # budget_usd -- an iteration-count artifact, not real futility. +40 per
    # resume mirrors how budget_usd already grows here.
    new_max_iterations = values.get("max_iterations", 40) + 40
    # Explicit even though initial_state() already sets this for any task
    # created after task_id was added to AgentState -- this closes the gap
    # for older tasks that predate that field.
    patch = {"task_id": task_id, "budget_usd": new_budget, "max_iterations": new_max_iterations}
    if was_escalated:
        budget_note = (
            f"Additional budget granted -- ${req.additional_budget_usd:.2f} more, ${new_budget:.2f} total now. "
            if req.additional_budget_usd > 0
            else "No additional budget added. "
        )
        resume_note = (
            f"Resumed by operator after escalation (was: {values.get('escalation_reason') or 'unknown reason'}). "
            f"{budget_note}"
            "Continue the task from where you left off."
        )
        if req.message:
            resume_note += f"\n\nOperator note: {req.message}"
        patch["escalated"] = False
        patch["escalation_reason"] = None
        patch["pending_feedback"] = resume_note
        patch["no_diff_streak"] = 0  # fresh attempt -- don't inherit a streak from before the escalation
        # Same reasoning as no_diff_streak: an operator resume is a fresh
        # attempt. Without this, a task escalated for a maxed stale streak
        # resumes with that streak still at the limit and re-escalates on
        # its very first quiet pass -- zero real runway.
        patch["stale_pending_review_streak"] = 0
        await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
    elif was_done:
        # Same as_node-forces-re-routing mechanism as the escalated branch --
        # _route_after_verify checks escalated (False here) then
        # pending_approval (None) then pending_feedback, so a truthy
        # pending_feedback alone is enough to route back to "work".
        # no_diff_streak must reset to 0: it's sitting at 2 (that's exactly
        # what triggered "done" in the first place) -- without resetting it,
        # a work pass that produces no diff for any reason (including the
        # agent legitimately needing one more read-only turn before it can
        # act on the operator's note) would immediately re-trigger the same
        # "done, no changes needed" verdict before the nudge had a real
        # chance to land.
        patch["pending_feedback"] = f"Operator note: {req.message}"
        patch["no_diff_streak"] = 0
        await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
    else:
        # Orphaned/stopped resume -- an operator message here goes through
        # the same mailbox work_node already drains on its own next pass
        # (see agent/messages.py, work.py), not baked into this patch --
        # unlike the escalated branch, there's no synthetic pending_feedback
        # already being constructed here to fold it into, and routing must
        # stay untouched (see this function's own docstring).
        if req.message:
            add_message(task_id, req.message)
        await graph.aupdate_state(thread_config, patch)

    _running_tasks[task_id] = asyncio.create_task(
        _stream_graph(task_id, values["repo"], values["goal"], new_budget, None)
    )
    return {"ok": True, "new_budget_usd": new_budget, "new_max_iterations": new_max_iterations}


@app.get("/api/tasks/{task_id}/diff")
async def get_task_diff(task_id: str, user: User = Depends(require_full_auth)):
    """The task's current diff against its branch point -- committed AND
    uncommitted work, plus untracked files. Serves both halves of the diff
    panel: polled live while the task runs (watch the agent's edits land),
    and rendered as the final look when the task parks on awaiting_merge.
    Read-only; nothing from the request reaches a command line (repo resolves
    through PROJECTS, git output is parsed server-side)."""
    repo = await _resolve_task_repo(task_id)
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    from agent.task_diff import collect_task_diff
    return await collect_task_diff(repo)


class MergeDecisionRequest(BaseModel):
    decision: str  # "approve" | "request_changes"
    message: str | None = None


@app.post("/api/tasks/{task_id}/merge-decision")
async def merge_decision(task_id: str, req: MergeDecisionRequest, user: User = Depends(require_full_auth)):
    """The operator's final-look verdict on a task parked at awaiting_merge.

    approve -> patch merge_approved_sha to the EXACT sha the operator was
    shown and re-invoke the graph: _route_after_verify sees approved+
    outstanding and runs verify_and_ship again, so the one code path that
    knows how to merge/record/finalize does the merge. The sha equality in
    that gate is what makes this race-safe -- approval can never ship a
    commit the operator didn't look at.

    request_changes -> the operator's notes become pending_feedback, exactly
    the shape a review-service rejection produces, so the agent loops back
    into work with the notes as its next instruction.
    """
    if task_id in _running_tasks:
        raise HTTPException(409, "task is already running")
    graph = app.state.graph
    thread_config = {"configurable": {"thread_id": task_id}}
    checkpoint = await graph.aget_state(thread_config)
    if not checkpoint or not checkpoint.values:
        raise HTTPException(404, "task not found")
    values = checkpoint.values
    check_repo_access(user, values["repo"])

    pending = values.get("pending_merge_approval")
    if not pending:
        raise HTTPException(409, "task is not awaiting a merge decision")

    if req.decision == "approve":
        patch = {
            "merge_approved_sha": pending["sha"],
            "pending_merge_approval": None,
        }
    elif req.decision == "request_changes":
        if not (req.message or "").strip():
            raise HTTPException(400, "request_changes requires a message -- the agent needs to know what to change")
        patch = {
            "pending_merge_approval": None,
            "merge_approved_sha": None,
            "pending_feedback": (
                "The operator reviewed the final diff and sent it back for more work "
                "before it may merge. Their notes:\n\n" + req.message.strip()
            ),
            # Fresh attempt, same reasoning as resume_task's escalated branch.
            "no_diff_streak": 0,
            "stale_pending_review_streak": 0,
        }
    else:
        raise HTTPException(400, "decision must be 'approve' or 'request_changes'")

    await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
    _running_tasks[task_id] = asyncio.create_task(
        _stream_graph(task_id, values["repo"], values["goal"], values.get("budget_usd", 0.0), None)
    )
    return {"ok": True, "decision": req.decision}


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, req: ApprovalRequest, user: User = Depends(require_full_auth)):
    """Submits an operator decision on a pending human-in-the-loop approval
    request (deep_agent.py's INTERRUPT_ON -- a bash/write/edit call the
    coordinator or a subagent proposed that matched a sensitive-path/
    dangerous-command pattern). Structurally the same resume mechanism
    resume_task's escalated branch uses -- patch state `as_node=
    "verify_and_ship"` with a truthy `pending_feedback` so
    `_route_after_verify` routes to "work", and let work_node's own
    graph_input priority logic (approval_decision checked before
    pending_feedback -- see work.py's own docstring case 0) do the real
    work of constructing `Command(resume={"decisions": [...]})` against the
    same inner thread, resuming it exactly where interrupt() paused it.

    One decision applies uniformly to every action_request in this batch
    (approve-all or reject-all) -- deepagents' own protocol technically
    supports a distinct decision per action_request, but the model
    virtually always proposes one risky call at a time in practice, and a
    per-action-request UI is real added complexity for a case that's
    rare enough not to justify it in this first pass.
    """
    if task_id in _running_tasks:
        raise HTTPException(409, "task is already running")
    graph = app.state.graph
    thread_config = {"configurable": {"thread_id": task_id}}
    checkpoint = await graph.aget_state(thread_config)
    if not checkpoint or not checkpoint.values:
        raise HTTPException(404, "task not found")
    values = checkpoint.values
    check_repo_access(user, values["repo"])

    pending = values.get("pending_approval")
    if not pending:
        raise HTTPException(409, "task has no pending approval request")

    action_count = len(pending.get("action_requests") or [])
    if req.decision == "approve":
        decisions = [{"type": "approve"} for _ in range(action_count)]
    elif req.decision == "respond":
        # ask_user answers: the operator's text IS the tool result (the
        # library's native "ask user"-style-tool pattern -- see deep_agent's
        # INTERRUPT_ON["ask_user"]). A respond without text is meaningless.
        if not (req.message or "").strip():
            raise HTTPException(400, "respond decision requires a message (the answer)")
        decisions = [{"type": "respond", "message": req.message} for _ in range(action_count)]
    else:
        reject = {"type": "reject"}
        if req.message:
            reject["message"] = req.message
        decisions = [dict(reject) for _ in range(action_count)]

    patch = {
        "task_id": task_id,
        "pending_approval": None,
        "approval_decision": decisions,
        # Placeholder text, never actually sent to the model -- work_node's
        # graph_input logic checks approval_decision first and uses that
        # instead whenever it's set (see work.py case 0). This exists
        # purely so _route_after_verify's existing "pending_feedback set ->
        # route to work" check fires, reusing that already-proven mechanism.
        "pending_feedback": "[operator submitted an approval decision]",
    }
    await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")

    _running_tasks[task_id] = asyncio.create_task(
        _stream_graph(task_id, values["repo"], values["goal"], values["budget_usd"], None)
    )
    return {"ok": True, "decision": req.decision}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, repo: str, user: User = Depends(require_full_auth)):
    check_repo_access(user, repo)
    store = app.state.store
    meta = await _read_with_retry(lambda: store.aget(("tasks", repo), task_id))
    if not meta:
        raise HTTPException(404, "task not found")
    thread_config = {"configurable": {"thread_id": task_id}}
    checkpoint = await _read_with_retry(lambda: app.state.graph.aget_state(thread_config))
    values = checkpoint.values if checkpoint else None
    # Same "orphaned" condition resume_task already accepts (store says
    # "running" but nothing is actually driving it -- e.g. a backend restart
    # mid-run) -- surfaced here too so the frontend can offer Resume instead
    # of leaving the task looking merely slow forever.
    orphaned = bool(
        meta.value.get("status") == "running"
        and task_id not in _running_tasks
        and not (values or {}).get("escalated")
    )
    # _state_snapshot_for_frontend translates latest_todos -> plan (the
    # AgentState has no `plan` key at all -- see that function's own
    # docstring); without it, a page load/reconnect would show an empty
    # plan until the next live "todos" event happened to arrive.
    snapshot = _apply_plan_fallback(_state_snapshot_for_frontend(values) if values else None, meta.value)
    if snapshot is not None:
        # Cost freshness on hydrate (2026-08-28): the checkpoint's
        # cost_so_far only advances when a work pass RETURNS, so a refresh
        # mid-pass showed a stale number until the next live tick. The meta
        # mirror tracks live spend -- take whichever is higher (cost only
        # ever grows within a run).
        _meta_cost = meta.value.get("cost_so_far")
        if isinstance(_meta_cost, (int, float)) and _meta_cost > (snapshot.get("cost_so_far") or 0):
            snapshot["cost_so_far"] = _meta_cost
        # Full detailed history across refresh/task-switch -- see the live-log
        # buffer's own comment. The checkpoint's execution_log (per-pass
        # summaries) stays the durable fallback.
        snapshot["execution_log"] = _fuller_log(_live_task_log.get(task_id), snapshot.get("execution_log"))
    return {"meta": meta.value, "state": snapshot, "orphaned": orphaned}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, repo: str, user: User = Depends(require_full_auth)):
    """Removes a finished task from the list entirely -- for work that was
    completed by hand (e.g. an operator finished it directly and merged/
    deployed outside the agent), where leaving it sitting as "stopped"
    forever would be misleading clutter, not a useful record. Refuses to
    delete a task that's still actively running -- stop it first, same as
    any other state-changing action here.
    """
    check_repo_access(user, repo)
    if task_id in _running_tasks:
        raise HTTPException(409, "task is running -- stop it before deleting")
    store = app.state.store
    meta = await store.aget(("tasks", repo), task_id)
    if not meta:
        raise HTTPException(404, "task not found")
    await store.adelete(("tasks", repo), task_id)
    await app.state.checkpointer.adelete_thread(task_id)
    # Also delete the inner deep-agent thread's own checkpoints. Every "work"
    # pass runs the deep agent against a derived thread_id, f"{task_id}:work"
    # (see work.py's inner_thread_config), on the same checkpointer/DB as
    # the outer graph -- deleting only the outer task_id's thread would leave
    # that inner thread's checkpoints orphaned in the checkpoints table.
    await app.state.checkpointer.adelete_thread(f"{task_id}:work")
    # Fresh-restart generations (work.py's inner_thread_config with
    # generation > 0 -- see outer_state.py's inner_thread_generation) get
    # their own derived thread_ids too; delete those as well or they'd be
    # orphaned exactly the way the base :work thread would be. The actual
    # generation count lives in the outer thread's now-deleted checkpoint,
    # so sweep a bounded range instead -- adelete_thread on a nonexistent
    # thread is a harmless no-op, and MAX_THREAD_RESTARTS (currently 1)
    # keeps real generations far below this bound.
    for generation in range(1, 10):
        await app.state.checkpointer.adelete_thread(f"{task_id}:work:g{generation}")
    for q, ws in _subscribers.pop(task_id, []):
        try:
            await ws.close(code=4000, reason="task deleted")
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/model-config")
async def get_model_config(user: User = Depends(require_full_auth)):
    """Current pins for this agent's own seven roles only -- see
    model_config.MANAGED_ROLES. Everything else in llm-router/config.yaml
    (the shared tier system, reasoning-tier, smart-router) belongs to
    the review service and is never exposed here.
    """
    auth.require_admin(user)
    # Live catalog prices, not the hand-written model_info blocks (which drift).
    return {"roles": await model_config.get_current_pins_priced()}


@app.get("/api/model-config/catalog")
async def get_model_catalog(refresh: bool = False, user: User = Depends(require_full_auth)):
    """OpenRouter's live model catalog for the picker's dropdown -- cached
    for 10 minutes; pass ?refresh=true to force a fresh fetch."""
    auth.require_admin(user)
    stats = model_config.forced_tool_call_stats()
    catalog = await model_config.fetch_model_catalog(force=refresh)
    return {
        "models": catalog,
        # Roles that force a tool call cannot use every model, and OpenRouter's
        # catalog cannot tell you which -- supported_parameters lists tool_choice
        # and reasoning separately while some providers refuse the COMBINATION.
        # These lists come from scripts/probe_forced_tool_call.py making real
        # requests, so the picker can hide models that would fail.
        "forced_tool_call": {**stats, "catalog_size": len(catalog)},
    }


@app.post("/api/model-config")
async def save_model_config(req: SaveModelPinsRequest, user: User = Depends(require_full_auth)):
    """Writes new pins for one or more of this agent's own roles. Does NOT
    restart llm-router -- the change only takes effect once that's done
    separately via POST /api/model-config/restart, since that restart
    affects every consumer of the shared router, not just this agent, and
    should never be an automatic side effect of a save.
    """
    auth.require_admin(user)
    catalog = await model_config.fetch_model_catalog()
    try:
        changed = model_config.set_pins(req.pins, catalog)
    except model_config.UnknownRoleError as e:
        raise HTTPException(400, str(e))
    except model_config.ModelNotInCatalogError as e:
        raise HTTPException(400, str(e))
    except model_config.PinBlockNotFoundError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "changed": changed, "roles": await model_config.get_current_pins_priced()}


@app.get("/api/model-config/endpoints")
async def get_model_endpoints(model: str, user: User = Depends(require_full_auth)):
    """The providers currently serving `model` on OpenRouter -- feeds the
    dashboard's provider picker. Names returned here are exactly what
    provider pinning writes into `provider.only`."""
    auth.require_admin(user)
    try:
        return {"model": model, "endpoints": await model_config.fetch_model_endpoints(model)}
    except Exception as e:  # noqa: BLE001 -- surface upstream failures as a readable 502
        raise HTTPException(502, f"could not fetch providers for {model!r}: {e}")


class SaveProviderPinsRequest(BaseModel):
    # role -> provider name, or null/"" to clear back to auto-routing
    pins: dict[str, str | None]


@app.post("/api/model-config/providers")
async def save_provider_pins(req: SaveProviderPinsRequest, user: User = Depends(require_full_auth)):
    """Pin (or clear) the OpenRouter provider per role. Same contract as the
    model pin save: writes config.yaml, takes effect at the next router
    restart, which stays a separate explicit action."""
    auth.require_admin(user)
    cleaned = {r: (p or None) for r, p in req.pins.items()}
    try:
        model_config.set_provider_pins(cleaned)
    except model_config.UnknownRoleError as e:
        raise HTTPException(400, str(e))
    except model_config.ProviderPinError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "roles": await model_config.get_current_pins_priced()}


# ---------------------------------------------------------------------------
# Project onboarding wizard (agent/provisioning.py)
#
# Admin-only, and deliberately three separate calls -- detect, then confirm,
# then provision. The middle step is not ceremony: detection can propose a
# check command that would run against a live production service, and the
# only reliable filter for that is a human who knows the system. See
# agent/provisioning.py's docstring.
# ---------------------------------------------------------------------------


class DetectProjectRequest(BaseModel):
    path: str


class ProvisionProjectRequest(BaseModel):
    # Only the path and the operator's answers cross the wire. `live` and
    # `sandbox` are deliberately NOT accepted: they were a filesystem write
    # primitive supplied by the client. The server re-runs detection and
    # derives both, then confirms the answers are a subset of what it just
    # proposed (agent/provisioning.validate_choices).
    path: str
    choices: dict
    grant_access: bool = True


@app.get("/api/projects")
async def list_projects_config(user: User = Depends(require_full_auth)):
    """Every configured project with its full entry -- the wizard's landing
    view. Admin-only because the entries name host paths and credential
    filenames."""
    auth.require_admin(user)
    from agent.config import _PROJECTS_CONFIG_PATH  # noqa: PLC0415

    return {
        "projects": PROJECTS,
        "config_path": str(_PROJECTS_CONFIG_PATH),
        "restart_required_hint": (
            "projects.json is read at process start, so a newly added project "
            "becomes selectable after the agent restarts."
        ),
    }


@app.post("/api/projects/detect")
async def detect_project_endpoint(req: DetectProjectRequest, user: User = Depends(require_full_auth)):
    """Read-only inspection of a candidate directory. Creates nothing."""
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    try:
        report = await asyncio.to_thread(
            provisioning.detect_project, req.path.strip(), existing_names=list(PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    return report.to_dict()


@app.post("/api/projects/provision")
async def provision_project_endpoint(req: ProvisionProjectRequest, user: User = Depends(require_full_auth)):
    """Create the worktree, write the config entry, and seed the agent's
    knowledge for this project. Each step reports independently: a failure
    after the worktree exists must not read as "nothing happened".
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415
    from agent.config import _PROJECTS_CONFIG_PATH  # noqa: PLC0415

    # Re-detect rather than trust the client's copy of the report: between the
    # wizard's two calls the directory may have changed, and a hand-made
    # request could otherwise assert facts (paths, commands) the server never
    # established.
    try:
        report = await asyncio.to_thread(
            provisioning.detect_project, req.path.strip(), existing_names=list(PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    if report.blockers:
        raise HTTPException(400, "; ".join(report.blockers))

    name = report.name
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(400, "invalid project name")
    if name in PROJECTS:
        raise HTTPException(400, f"{name!r} is already configured")

    try:
        choices = provisioning.validate_choices(report, req.choices)
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))

    steps: list[dict] = []

    def _step(label: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": label, "ok": ok, "detail": detail})

    logger.info("onboarding: %s provisioning %s from %s", user.email, name, report.live)

    ok, detail = await asyncio.to_thread(provisioning.create_worktree, report.live, report.sandbox)
    _step("worktree", ok, detail)
    if not ok:
        return {"ok": False, "steps": steps}

    entry = provisioning.config_from_choices(name, report.live, report.sandbox, choices)
    try:
        await asyncio.to_thread(provisioning.write_project_entry, _PROJECTS_CONFIG_PATH, name, entry)
        _step("config", True, f"wrote {name} to projects.json")
    except (provisioning.ProvisioningError, OSError, ValueError) as e:
        _step("config", False, str(e))
        return {"ok": False, "steps": steps}

    # Load the new entry into the RUNNING process. Without this the project
    # exists in projects.json and nowhere else -- every consumer holds the
    # dict read at import, so the cartographer below would KeyError on it and
    # the project would stay invisible until a restart.
    from agent.config import reload_projects  # noqa: PLC0415

    reload_projects()
    _step("reload", True, f"{len(PROJECTS)} projects now live in this process")

    # Knowledge seeding. Best-effort by design: a project whose map failed to
    # build is still a usable project and the operator can re-run the
    # cartographer. Never fail the whole onboarding over it.
    try:
        from agent.deep_agent import seed_memory  # noqa: PLC0415

        starter = (
            f"# {name} project memory\n\n"
            "Durable, cross-task facts about this project. The consolidator appends what "
            "it learns from completed tasks; add anything an agent must know before "
            "touching this repo.\n"
        )
        await seed_memory(name, app.state.store, starter)
        _step("memory", True, "seeded starter project memory")
    except Exception as e:  # noqa: BLE001 -- reported, never fatal
        _step("memory", False, str(e))

    try:
        summary = await cartographer.run_cartographer(config, name, app.state.store, force=True)
        _step("codebase-map", True, str(summary)[:300])
    except Exception as e:  # noqa: BLE001
        _step("codebase-map", False, f"{e} -- run scripts/run_cartographer.py {name} later")

    if req.grant_access and user.allowed_repos is not None:
        try:
            await auth.update_user_access(app.state.auth_pool, user.id,
                                          [*user.allowed_repos, name])
            _step("access", True, f"granted {user.email} access to {name}")
        except Exception as e:  # noqa: BLE001
            _step("access", False, str(e))

    return {
        "ok": True,
        "steps": steps,
        "message": f"{name} is configured and live in this process.",
    }


class SaveEnvKeysRequest(BaseModel):
    updates: dict[str, str]


@app.get("/api/env-config")
async def get_env_config(user: User = Depends(require_full_auth)):
    """The credentials this deployment runs on, MASKED.

    There is deliberately no endpoint that returns a secret's value. Reads give
    the last four characters and whether it is set, which is enough to confirm
    *which* key is installed without being enough to use it.
    """
    auth.require_admin(user)
    return {"keys": env_config.list_keys()}


@app.post("/api/env-config")
async def save_env_config(req: SaveEnvKeysRequest, user: User = Depends(require_full_auth)):
    """Write new values for allow-listed keys.

    Restarts are reported, not performed: restarting the router interrupts every
    in-flight model call, and that is the operator's call to make, not a side
    effect of saving a form.
    """
    auth.require_admin(user)
    try:
        result = env_config.set_keys(req.updates)
    except env_config.UnknownKeyError as e:
        # The key NAME is safe to echo; the value never is.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("env-config write failed for %s: %s", sorted(req.updates), type(e).__name__)
        raise HTTPException(status_code=500, detail="could not write the env file")
    logger.info("env-config updated by %s: %s", user.email, ", ".join(result["updated"]))
    return result


@app.post("/api/env-config/restart")
async def restart_services(req: SaveEnvKeysRequest, user: User = Depends(require_full_auth)):
    """Restart the named services so a key change takes effect."""
    auth.require_admin(user)
    import asyncio as _a
    allowed = {"llm-router", "3d-agent", "commit-reviewer", "agent-review"}
    names = [n for n in (req.updates.get("services", "") or "").split(",") if n.strip() in allowed]
    if not names:
        raise HTTPException(status_code=400, detail="no known services named")
    out = {}
    for n in names:
        # 3d-agent restarting kills this request mid-flight, which is expected —
        # the client treats a dropped connection on its own restart as success.
        proc = await _a.create_subprocess_exec(
            "pm2", "restart", n, stdout=_a.subprocess.PIPE, stderr=_a.subprocess.STDOUT)
        try:
            o, _ = await _a.wait_for(proc.communicate(), timeout=60)
            out[n] = "ok" if proc.returncode == 0 else (o or b"").decode()[-200:]
        except _a.TimeoutError:
            proc.kill()
            out[n] = "timed out"
    return {"restarted": out}


@app.get("/api/consolidation/status")
async def consolidation_status(user: User = Depends(require_full_auth)):
    """Last nightly memory-consolidation run: when, and whether it succeeded.

    Exists because a failed run used to be indistinguishable from a healthy one
    — the script printed a line and exited 0, so cron stayed quiet and a provider
    incompatibility skipped consolidation unnoticed for months. The marker file
    is written by scripts/consolidation-cron.sh on every run.
    """
    auth.require_admin(user)
    root = Path(__file__).resolve().parent.parent
    marker = root / "data" / "last_consolidation.json"
    log = Path("/var/log/agent-consolidation.log")

    # stale defaults False, not True: a default of True combined with a silent
    # except-pass meant any error here rendered a healthy run as STALE. That is
    # the same silent-failure shape this panel exists to catch, so failures to
    # parse are reported as `stale_error` rather than swallowed into a verdict.
    payload: dict = {"ran_at": None, "ok": None, "exit_code": None, "stale": False, "tail": ""}
    try:
        payload.update(json.loads(marker.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        payload["marker_error"] = str(e)[:200]

    # Stale = no run in over 48h. The job is nightly, so one missed night is
    # worth surfacing rather than waiting for someone to read a log.
    if payload.get("ran_at"):
        try:
            ran = _dt.fromisoformat(str(payload["ran_at"]).replace("Z", "+00:00"))
            age_h = (_dt.now(_tz.utc) - ran).total_seconds() / 3600
            payload["age_hours"] = round(age_h, 1)
            payload["stale"] = age_h > 48
        except Exception as e:  # noqa: BLE001
            payload["stale_error"] = f"could not parse ran_at: {e}"[:200]

    try:
        payload["tail"] = "\n".join(log.read_text(errors="replace").splitlines()[-40:])
    except Exception:
        pass
    return payload


@app.post("/api/model-config/probe-forced-tool-call")
async def probe_forced_tool_call(user: User = Depends(require_full_auth)):
    """Re-run the forced-tool-call probe and refresh the picker's allow-list.

    Worth a button because the answer genuinely goes stale: OpenRouter adds and
    retires models constantly, and compliance is per-provider -- the same model
    can pass or fail depending on who answers, so a cached verdict decays. This
    runs the real probe (scripts/probe_forced_tool_call.py) rather than re-reading
    the catalog, because the catalog cannot express the constraint.
    """
    auth.require_admin(user)
    import asyncio as _asyncio

    script = Path(__file__).resolve().parent.parent / "scripts" / "probe_forced_tool_call.py"
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    proc = await _asyncio.create_subprocess_exec(
        str(venv_py), str(script), "--all", "--concurrency", "8",
        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await _asyncio.wait_for(proc.communicate(), timeout=1500)
    except _asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="probe timed out")

    stats = model_config.forced_tool_call_stats()
    return {
        "ok": proc.returncode == 0,
        **stats,
        "catalog_size": len(await model_config.fetch_model_catalog()),
        "tail": (out or b"").decode(errors="replace")[-1200:],
    }


@app.post("/api/model-config/restart-router")
def restart_model_router(user: User = Depends(require_full_auth)):
    """Restarts llm-router so a saved pin change actually takes effect.
    Shared-impact action: this restarts the same router the
    review service depend on, not just this agent -- the frontend must
    surface that plainly rather than bundling this into save.
    """
    auth.require_admin(user)
    result = model_config.restart_llm_router()
    if not result["ok"]:
        raise HTTPException(500, result["output"] or "pm2 restart failed")
    return result


@app.websocket("/api/tasks/{task_id}/stream")
async def stream_task(ws: WebSocket, task_id: str):
    user = await auth.get_user_from_ws_cookie(app.state.auth_pool, ws.cookies)
    if not user:
        await ws.close(code=4401)
        return
    # audit H-1: same forced-screen enforcement as the planning stream above.
    if _forced_screen_block(user):
        await ws.close(code=4403)
        return
    task_repo = await _resolve_task_repo(task_id)
    # Fail CLOSED, same reasoning as the REST endpoints above: an
    # unresolvable repo previously meant "no check", which let any
    # authenticated user attach to the stream.
    if not task_repo or not user.can_access(task_repo):
        await ws.close(code=4403)
        return
    await ws.accept()

    # Every connection for this task_id gets its own queue and stays
    # subscribed for its own lifetime -- multiple viewers (different users,
    # or the same user in two tabs) are simultaneously live on the same
    # task, all receiving the same fan-out from _publish(). This used to
    # evict every prior connection the moment a new one arrived ("only the
    # newest connection should ever receive events"), which was fine when
    # this system had exactly one operator but broke outright the moment a
    # second real user existed -- whoever opened the task last would silently
    # kick everyone else's live view. Each connection's own receiver() below
    # still detects and cleans up ITS OWN disconnect independently, so a
    # genuinely stale/dead connection is removed on its own without needing
    # to evict anyone else's.
    # audit M-34: bounded, so a stalled-but-connected reader can't accumulate
    # every event of a multi-hour task in memory (_publish drops on overflow).
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
    _subscribers.setdefault(task_id, []).append((queue, ws))

    async def sender():
        while True:
            # Heartbeat (2026-08-28): a long model call can mean 60s+ of
            # total socket silence, and NAT/middleboxes between the operator
            # and this VPS kill idle TCP without telling either end -- the
            # browser's stream just goes still until a manual refresh
            # (reported live). A ping every 20s keeps bytes flowing through
            # every hop, and when the socket IS dead, send_json raises here
            # promptly so the client gets a real close event to react to
            # instead of silence. Clients ignore the "ping" type.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(event)
            if event.get("type") == "closed":
                break

    async def receiver():
        # The client never actually sends anything on this socket -- this
        # exists purely to detect a disconnect promptly. `await queue.get()`
        # in sender() has no way to notice the client closed the connection;
        # it only finds out lazily, the next time it tries to send and that
        # fails. Between those two points the stale queue stays subscribed,
        # which is harmless now beyond a handful of buffered events never
        # being delivered anywhere -- the `finally` block below still always
        # removes exactly this one connection's own entry, never another
        # viewer's.
        while True:
            await ws.receive()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        # asyncio.wait() does not propagate exceptions from the tasks it
        # waits on (unlike awaiting a task directly) -- WebSocketDisconnect
        # from receiver() would otherwise never surface anywhere and could
        # log an "exception was never retrieved" warning. Retrieving it here
        # (without re-raising -- a disconnect is the expected, normal way
        # this ends) marks it handled either way.
        for task in done:
            task.exception()
        for task in pending:
            task.cancel()
    finally:
        entry = (queue, ws)
        if entry in _subscribers.get(task_id, []):
            _subscribers[task_id].remove(entry)
        # audit M-34: drop the now-empty list so the dict doesn't keep one stale
        # key per task forever.
        if task_id in _subscribers and not _subscribers[task_id]:
            del _subscribers[task_id]


# Static frontend, mounted last so it never shadows an /api/* route above.
# A single-page app with one real route today, but the catch-all fallback
# means adding client-side routes later won't need a matching nginx change.
#
# Cache headers are the whole reason this isn't just a bare StaticFiles
# mount. Nothing here sent any Cache-Control at all, which does NOT mean
# "don't cache" -- with only a Last-Modified to go on, browsers fall back to
# heuristic caching and are free to reuse index.html for a while. index.html
# is the file that names the content-hashed bundle, so a stale copy of it
# pins the browser to the PREVIOUS deploy's JS/CSS: new code ships, the
# server serves it correctly, and the operator still sees the old UI until
# they happen to hard-reload. Confirmed live 2026-08-23 (this exact bug: a
# rebuilt chat composer was being served and simply never appeared).
#
# The fix is the standard split for hashed-asset SPAs:
#   * index.html      -> no-store. Never reused; every load re-reads which
#                        bundle is current. It's ~400 bytes, so revalidating
#                        it on each load costs nothing.
#   * /assets/*       -> immutable, one year. Safe precisely BECAUSE the
#                        filename contains a content hash -- different
#                        content is always a different URL, so a cached copy
#                        can never be stale. This is what keeps the split
#                        cheap: the tiny file is always fetched, the 690KB
#                        one is cached hard.
class _ImmutableAssets(StaticFiles):
    """StaticFiles that marks content-hashed bundles permanently cacheable."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


if FRONTEND_DIST.is_dir():
    app.mount("/assets", _ImmutableAssets(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # Real files at the dist root — favicons, the apple-touch icon — are
        # served as themselves. Before this, ONLY /assets was mounted and every
        # other path fell through to index.html, so the favicon <link>s fetched
        # 200 text/html and the tab never showed an icon (silently: a 200 with
        # the wrong body looks fine in every log). Resolved-and-contained check
        # rather than trusting the path: `..` segments must not escape dist.
        if full_path:
            candidate = (FRONTEND_DIST / full_path).resolve()
            if (
                candidate.is_file()
                and candidate.is_relative_to(FRONTEND_DIST.resolve())
                and candidate.parent == FRONTEND_DIST.resolve()
            ):
                # Stable names (not content-hashed), so a day, not a year —
                # same split the trading bot's nginx uses for its own icons.
                return FileResponse(candidate, headers={"cache-control": "public, max-age=86400"})
        return FileResponse(
            FRONTEND_DIST / "index.html",
            headers={"cache-control": "no-store, must-revalidate"},
        )
