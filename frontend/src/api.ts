import type { ModelStats, RepoStats, RouterBalance, TaskMeta, TaskState, Analytics, AgentModelUsage, ModelPin, ModelCatalogEntry, ToolReliability, TraceSummary, PlanningSessionMeta, PlanningLogEntry, CurrentUser } from "./types";

// import.meta.env.BASE_URL is Vite's own `base` config value ("/" in dev,
// a subpath in the production build -- see vite.config.ts). Building
// API_BASE from it means this file doesn't need separate dev/prod branches:
// dev's proxy and prod's nginx location both forward whatever lands on
// {BASE_URL}api to the same backend.
const API_BASE = `${import.meta.env.BASE_URL.replace(/\/$/, "")}/api`;

// audit H-14: a typed error for an expired/invalid session, so callers and the
// central handler can distinguish "you are logged out" from any other failure.
// Previously every wrapper threw a generic Error with the status in a string,
// so an expired cookie degraded into stale data and opaque "listTasks failed:
// 401" messages with no path back to the login screen.
export class AuthError extends Error {
  constructor(message = "session expired") {
    super(message);
    this.name = "AuthError";
  }
}

// App registers a handler here so a 401 on ANY authenticated request clears the
// user and drops back to the login screen, from one place instead of ~50.
let onAuthFailure: (() => void) | null = null;
export function setAuthFailureHandler(fn: (() => void) | null): void {
  onAuthFailure = fn;
}

// Drop-in replacement for fetch on every AUTHENTICATED endpoint. A 401 fires the
// global handler and throws AuthError; everything else is returned unchanged for
// each caller's own res.ok handling. Pre-auth endpoints (login, 2FA verify,
// logout, forgot/reset password) deliberately stay on raw fetch -- a 401 there
// means "bad credentials", not "session expired", and must not trigger logout.
// audit M-20: a default request timeout so a hung request (an intermediary
// silently holding the connection) fails with a clear error instead of spinning
// forever. Callers that legitimately run long (probeForcedToolCall) pass a
// larger `timeoutMs`; a caller supplying its own AbortSignal opts out.
const DEFAULT_TIMEOUT_MS = 60_000;

async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const signal = init?.signal ?? AbortSignal.timeout(timeoutMs);
  let res: Response;
  try {
    // NOTE: this MUST call the global fetch, not apiFetch -- an earlier edit
    // accidentally made it recurse into itself, stack-overflowing every
    // authenticated call.
    res = await fetch(input, { ...init, signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  }
  if (res.status === 401) {
    onAuthFailure?.();
    throw new AuthError();
  }
  return res;
}

// --- Auth --------------------------------------------------------------

export async function login(email: string, password: string): Promise<{ requires_2fa: boolean; temp_token?: string; user?: CurrentUser }> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `login failed: ${res.status}`);
  }
  return res.json();
}

export async function verify2FA(tempToken: string, code: string): Promise<{ user: CurrentUser }> {
  const res = await fetch(`${API_BASE}/auth/2fa/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ temp_token: tempToken, code }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `2FA verify failed: ${res.status}`);
  }
  return res.json();
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST" });
}

export async function getMe(): Promise<CurrentUser> {
  const res = await apiFetch(`${API_BASE}/auth/me`);
  if (!res.ok) throw new Error(`getMe failed: ${res.status}`);
  return res.json();
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/change-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `change password failed: ${res.status}`);
  }
}

export async function setAutoApprove(autoApproveCommands: boolean): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/me/auto-approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_approve_commands: autoApproveCommands }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saving auto mode failed: ${res.status}`);
  }
}

export async function forgotPassword(email: string): Promise<void> {
  await fetch(`${API_BASE}/auth/forgot-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  // Always "ok" from the backend's own side (anti-enumeration) -- nothing
  // meaningful to throw on here even for a non-2xx, so this never rejects.
}

export async function resetPassword(email: string, code: string, newPassword: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/reset-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, code, new_password: newPassword }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `reset password failed: ${res.status}`);
  }
}

export async function setup2FA(): Promise<{ secret: string; uri: string }> {
  const res = await apiFetch(`${API_BASE}/auth/2fa/setup`, { method: "POST" });
  if (!res.ok) throw new Error(`setup2FA failed: ${res.status}`);
  return res.json();
}

export async function confirm2FA(code: string): Promise<{ recovery_codes: string[] }> {
  const res = await apiFetch(`${API_BASE}/auth/2fa/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `confirm2FA failed: ${res.status}`);
  }
  return res.json();
}

export async function disable2FA(): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/2fa/disable`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `disable2FA failed: ${res.status}`);
  }
}

export async function listUsers(): Promise<CurrentUser[]> {
  const res = await apiFetch(`${API_BASE}/auth/users`);
  if (!res.ok) throw new Error(`listUsers failed: ${res.status}`);
  return res.json();
}

export async function createUser(email: string, password: string, role: string, allowedRepos: string[] | null): Promise<CurrentUser> {
  const res = await apiFetch(`${API_BASE}/auth/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, role, allowed_repos: allowedRepos }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `createUser failed: ${res.status}`);
  }
  return res.json();
}

export async function updateUserAccess(userId: number, allowedRepos: string[] | null): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ allowed_repos: allowedRepos }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `updateUserAccess failed: ${res.status}`);
  }
}

export async function deleteUser(userId: number): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/users/${userId}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `deleteUser failed: ${res.status}`);
  }
}

export async function listRepos(): Promise<string[]> {
  const res = await apiFetch(`${API_BASE}/repos`);
  if (!res.ok) throw new Error(`listRepos failed: ${res.status}`);
  return res.json();
}

export async function listTasks(repo?: string): Promise<TaskMeta[]> {
  const url = repo ? `${API_BASE}/tasks?repo=${encodeURIComponent(repo)}` : `${API_BASE}/tasks`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`listTasks failed: ${res.status}`);
  return res.json();
}

export async function getTask(taskId: string, repo: string): Promise<{ meta: TaskMeta; state: TaskState | null; orphaned: boolean }> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}?repo=${encodeURIComponent(repo)}`);
  if (!res.ok) throw new Error(`getTask failed: ${res.status}`);
  return res.json();
}

export interface AttachmentEntry {
  path: string;
  kind: string;
  bytes: number;
  extracted_text?: string | null;
  pages?: number;
  note?: string;
}

export async function uploadFiles(repo: string, files: File[]): Promise<AttachmentEntry[]> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const res = await apiFetch(`${API_BASE}/uploads?repo=${encodeURIComponent(repo)}`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `upload failed: ${res.status}`);
  }
  return (await res.json()).files;
}

export async function createTask(goal: string, repo: string, budgetUsd?: number, attachments?: AttachmentEntry[]): Promise<{ task_id: string }> {
  const res = await apiFetch(`${API_BASE}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal, repo, budget_usd: budgetUsd, attachments: attachments?.length ? attachments : null }),
  });
  if (!res.ok) throw new Error(`createTask failed: ${res.status}`);
  return res.json();
}

export async function sendTaskMessage(taskId: string, text: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) throw new Error(`sendTaskMessage failed: ${res.status}`);
}

export async function stopTask(taskId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/stop`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `stopTask failed: ${res.status}`);
  }
}

export async function getTaskDiff(taskId: string): Promise<import("./types").TaskDiff> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/diff`);
  if (!res.ok) throw new Error(`getTaskDiff failed: ${res.status}`);
  return res.json();
}

export async function submitMergeDecision(
  taskId: string,
  decision: "approve" | "request_changes",
  message?: string,
): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/merge-decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, message }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `merge decision failed: ${res.status}`);
  }
}

export interface TelegramSettings {
  configured: boolean;
  chat_id: string | null;
}

export async function getTelegramSettings(): Promise<TelegramSettings> {
  const res = await apiFetch(`${API_BASE}/auth/me/telegram`);
  if (!res.ok) throw new Error(`getTelegramSettings failed: ${res.status}`);
  return res.json();
}

export async function setTelegramSettings(botToken: string, chatId: string): Promise<TelegramSettings> {
  const res = await apiFetch(`${API_BASE}/auth/me/telegram`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bot_token: botToken, chat_id: chatId }),
  });
  if (!res.ok) throw new Error(`setTelegramSettings failed: ${res.status}`);
  return res.json();
}

export async function sendTelegramTest(): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/me/telegram/test`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `test send failed: ${res.status}`);
  }
}

export interface ProviderEndpoint {
  provider: string;
  context_length: number | null;
  input_cost_per_token: number;
  output_cost_per_token: number;
  quantization: string | null;
  uptime: number | null;
  latency_s: number | null;
  throughput_tps: number | null;
  implicit_caching: boolean;
}

export async function getModelEndpoints(modelId: string): Promise<ProviderEndpoint[]> {
  const res = await apiFetch(`${API_BASE}/model-config/endpoints?model=${encodeURIComponent(modelId)}`);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `provider list failed: ${res.status}`);
  }
  return (await res.json()).endpoints;
}

export async function saveProviderPins(pins: Record<string, string | null>): Promise<{ roles: Record<string, ModelPin> }> {
  const res = await apiFetch(`${API_BASE}/model-config/providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pins }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `provider save failed: ${res.status}`);
  }
  return res.json();
}

export async function setMergeReview(requireMergeReview: boolean): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/me/merge-review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ require_merge_review: requireMergeReview }),
  });
  if (!res.ok) throw new Error(`setMergeReview failed: ${res.status}`);
}

export async function stopPlanningTurn(sessionId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/stop`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `stopPlanningTurn failed: ${res.status}`);
  }
}

export async function resumeTask(taskId: string, additionalBudgetUsd: number, message?: string): Promise<{ new_budget_usd: number }> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ additional_budget_usd: additionalBudgetUsd, message: message || null }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `resumeTask failed: ${res.status}`);
  }
  return res.json();
}

export async function approveTask(taskId: string, decision: "approve" | "reject" | "respond", message?: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, message: message || null }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `approveTask failed: ${res.status}`);
  }
}

export async function deleteTask(taskId: string, repo: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}?repo=${encodeURIComponent(repo)}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `deleteTask failed: ${res.status}`);
  }
}

export function taskStreamUrl(taskId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${API_BASE}/tasks/${taskId}/stream`;
}

export async function getAgentModelUsage(): Promise<{ models: AgentModelUsage[] }> {
  const res = await apiFetch(`${API_BASE}/analytics/models`);
  if (!res.ok) throw new Error(`getAgentModelUsage failed: ${res.status}`);
  return res.json();
}

export async function getToolReliability(): Promise<ToolReliability> {
  const res = await apiFetch(`${API_BASE}/analytics/tool-reliability`);
  if (!res.ok) throw new Error(`getToolReliability failed: ${res.status}`);
  return res.json();
}

export async function getTraceSummary(): Promise<TraceSummary> {
  const res = await apiFetch(`${API_BASE}/analytics/trace-summary`);
  if (!res.ok) throw new Error(`getTraceSummary failed: ${res.status}`);
  return res.json();
}

export async function getAnalytics(): Promise<Analytics> {
  const res = await apiFetch(`${API_BASE}/analytics`);
  if (!res.ok) throw new Error(`getAnalytics failed: ${res.status}`);
  return res.json();
}

export async function getRepoStats(): Promise<RepoStats> {
  const res = await apiFetch(`${API_BASE}/stats`);
  if (!res.ok) throw new Error(`getRepoStats failed: ${res.status}`);
  return res.json();
}

// Proxied through this app's own backend (GET /api/router-balance) rather
// than calling the review service's own endpoint directly. A deployment may
// sit the review service behind a separate reverse-proxy auth that this
// app's users have no session for; calling it directly then silently 401s
// and the balance vanishes from the sidebar with no visible error.
export async function getRouterBalance(): Promise<RouterBalance> {
  const res = await apiFetch(`${API_BASE}/router-balance`);
  if (!res.ok) throw new Error(`getRouterBalance failed: ${res.status}`);
  return res.json();
}

export async function getModelStats(): Promise<{ models: ModelStats[] }> {
  const res = await apiFetch("/_review/api/router/stats");
  if (!res.ok) throw new Error(`getModelStats failed: ${res.status}`);
  return res.json();
}

// This agent's own seven pinned roles (agent/model_config.py's
// MANAGED_ROLES) -- distinct from the two above, which read the shared
// review-service router dashboard.
export async function getModelConfig(): Promise<{ roles: Record<string, ModelPin> }> {
  const res = await apiFetch(`${API_BASE}/model-config`);
  if (!res.ok) throw new Error(`getModelConfig failed: ${res.status}`);
  return res.json();
}

export interface ForcedToolCallInfo {
  compliant: string[];
  non_compliant: string[];
  /** Cannot be probed at all: batch-only endpoints, or no provider serving
   *  them under this account's data policy. Never becomes a pass. */
  unavailable?: string[];
  /** The probe itself was rate-limited (HTTP 429) — worth re-running. */
  transient?: string[];
  attempted?: number;
  catalog_size?: number;
  probed_at: string | null;
}

export interface ConsolidationStatus {
  ran_at: string | null;
  ok: boolean | null;
  exit_code: number | null;
  stale: boolean;
  age_hours?: number;
  tail: string;
}

export interface EnvKey {
  key: string;
  label: string;
  help: string;
  group: string;
  secret: boolean;
  is_set: boolean;
  /** Masked hint for secrets (last 4 chars), full value for non-secrets.
   *  There is no endpoint that returns a secret's real value. */
  display: string;
  restarts: string[];
  file: string;
}

export async function getEnvConfig(): Promise<{ keys: EnvKey[] }> {
  const res = await apiFetch(`${API_BASE}/env-config`);
  if (!res.ok) throw new Error(`getEnvConfig failed: ${res.status}`);
  return res.json();
}

export async function saveEnvConfig(
  updates: Record<string, string>,
): Promise<{ updated: string[]; restart_required: string[] }> {
  const res = await apiFetch(`${API_BASE}/env-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `save failed: ${res.status}`);
  return res.json();
}

export async function restartServices(services: string[]): Promise<{ restarted: Record<string, string> }> {
  const res = await apiFetch(`${API_BASE}/env-config/restart`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { services: services.join(",") } }),
  });
  if (!res.ok) throw new Error(`restart failed: ${res.status}`);
  return res.json();
}

export async function getConsolidationStatus(): Promise<ConsolidationStatus> {
  const res = await apiFetch(`${API_BASE}/consolidation/status`);
  if (!res.ok) throw new Error(`getConsolidationStatus failed: ${res.status}`);
  return res.json();
}

export async function probeForcedToolCall(): Promise<{
  ok: boolean;
  compliant: string[];
  non_compliant: string[];
  /** Cannot be probed at all: batch-only endpoints, or no provider serving
   *  them under this account's data policy. Never becomes a pass. */
  unavailable?: string[];
  /** The probe itself was rate-limited (HTTP 429) — worth re-running. */
  transient?: string[];
  attempted?: number;
  catalog_size?: number;
  probed_at: string | null;
  tail: string;
}> {
  const res = await apiFetch(`${API_BASE}/model-config/probe-forced-tool-call`, { method: "POST" }, 300_000);
  if (!res.ok) throw new Error(`probeForcedToolCall failed: ${res.status}`);
  return res.json();
}

export async function getModelCatalog(
  refresh = false,
): Promise<{ models: ModelCatalogEntry[]; forced_tool_call?: ForcedToolCallInfo }> {
  const res = await apiFetch(`${API_BASE}/model-config/catalog${refresh ? "?refresh=true" : ""}`);
  if (!res.ok) throw new Error(`getModelCatalog failed: ${res.status}`);
  return res.json();
}

export async function saveModelConfig(pins: Record<string, string>): Promise<{ roles: Record<string, ModelPin> }> {
  const res = await apiFetch(`${API_BASE}/model-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pins }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saveModelConfig failed: ${res.status}`);
  }
  return res.json();
}

export async function restartLlmRouter(): Promise<{ ok: boolean; output: string }> {
  const res = await apiFetch(`${API_BASE}/model-config/restart-router`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `restartLlmRouter failed: ${res.status}`);
  }
  return res.json();
}

export async function createPlanningSession(repo: string): Promise<{ session_id: string; repo: string }> {
  const res = await apiFetch(`${API_BASE}/planning/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo }),
  });
  if (!res.ok) throw new Error(`createPlanningSession failed: ${res.status}`);
  return res.json();
}

export async function listPlanningSessions(): Promise<PlanningSessionMeta[]> {
  const res = await apiFetch(`${API_BASE}/planning/sessions`);
  if (!res.ok) throw new Error(`listPlanningSessions failed: ${res.status}`);
  return res.json();
}

export async function getPlanningSession(
  sessionId: string,
): Promise<{ meta: PlanningSessionMeta; log: PlanningLogEntry[]; running: boolean }> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}`);
  if (!res.ok) throw new Error(`getPlanningSession failed: ${res.status}`);
  return res.json();
}

export async function archivePlanningSession(sessionId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/archive`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `archivePlanningSession failed: ${res.status}`);
  }
}

export async function sendPlanningMessage(sessionId: string, text: string, attachments?: AttachmentEntry[]): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, attachments: attachments?.length ? attachments : null }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `sendPlanningMessage failed: ${res.status}`);
  }
}

export function planningStreamUrl(sessionId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${API_BASE}/planning/sessions/${sessionId}/stream`;
}

// --- Project onboarding wizard (agent/provisioning.py) ----------------------

// Same shape the rest of this file uses inline: prefer FastAPI's `detail`,
// fall back to the status code.
async function errText(res: Response): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return (body as { detail?: string }).detail || `request failed: ${res.status}`;
}

export interface ProvisionCandidate {
  value: string;
  reason: string;
  enabled: boolean;
  warning: string | null;
}

export interface CheckStep {
  name: string;
  dir: string;
  cmd: string;
  args: string[];
  timeoutMs?: number;
}

export interface DetectionReport {
  name: string;
  live: string;
  sandbox: string;
  is_git_repo: boolean;
  package_manager: string | null;
  languages: string[];
  node_modules_dirs: string[];
  checks: CheckStep[];
  build_steps: CheckStep[];
  pm2_apps: ProvisionCandidate[];
  secret_files: ProvisionCandidate[];
  read_only_mounts: ProvisionCandidate[];
  risky_scripts: ProvisionCandidate[];
  db_env_file: string | null;
  warnings: string[];
  blockers: string[];
}

export interface ProvisionStep {
  step: string;
  ok: boolean;
  detail: string;
}

export interface ProvisionResult {
  ok: boolean;
  steps: ProvisionStep[];
  message?: string;
}

export async function listProjectsConfig(): Promise<{
  projects: Record<string, { live: string; sandbox: string }>;
  config_path: string;
}> {
  const res = await apiFetch(`${API_BASE}/projects`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function detectProject(path: string): Promise<DetectionReport> {
  const res = await apiFetch(`${API_BASE}/projects/detect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function provisionProject(body: {
  // The server re-detects from `path` and derives the live/worktree
  // locations itself -- the client cannot name them.
  path: string;
  choices: Record<string, unknown>;
  grant_access: boolean;
}): Promise<ProvisionResult> {
  const res = await apiFetch(`${API_BASE}/projects/provision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}
