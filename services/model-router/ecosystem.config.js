// pm2 app definition for the model router.
//
// Runs beside the litellm proxy on a different port until cutover, so both can
// be exercised against the same config.yaml before anything is switched.
// Cutover is one line -- point LITELLM_BASE_URL at this port. Rollback is the
// same line.
const fs = require('fs');
const path = require('path');

// The shared .env lives with the old router and holds both keys this needs.
// Read here rather than relying on pm2's env_file: it resolves relative to the
// pm2 daemon's cwd, not to this file, and a silently empty OPENROUTER_API_KEY
// turns into a service that starts, reports "degraded", and answers nothing.
function sharedEnv() {
  const out = {};
  try {
    const raw = fs.readFileSync(path.join(__dirname, '..', 'llm-router', '.env'), 'utf8');
    for (const line of raw.split('\n')) {
      const m = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
      if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch { /* readiness reports upstream_key:false, which is the honest answer */ }
  return out;
}

module.exports = {
  apps: [
    {
      name: 'model-router',
      cwd: __dirname,
      script: 'venv/bin/uvicorn',
      args: 'router.app:app --host 127.0.0.1 --port 4001 --workers 1',
      interpreter: 'none',
      // One worker on purpose: the config table and its hot reload are
      // per-process state, and a second worker would double every reload log
      // line while buying nothing -- the work is IO-bound on OpenRouter, which
      // one event loop handles far above this deployment's call rate (20
      // concurrent measured end to end in 4.9s).
      env: sharedEnv(),
      max_memory_restart: '500M',
      autorestart: true,
      time: true,
    },
  ],
};
