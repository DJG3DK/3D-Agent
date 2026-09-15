// pm2 app definition for the model router.
//
// Runs beside the litellm proxy on a different port until cutover, so both can
// be exercised against the same config.yaml and the same ledger before
// anything is switched. Cutover is one line: point LITELLM_BASE_URL at this
// port. Rollback is the same line.
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
      // line while buying nothing -- the work here is IO-bound on OpenRouter,
      // which a single event loop handles at far more than this deployment's
      // call rate (20 concurrent measured at 4.9s end to end).
      env_file: '../llm-router/.env',
      max_memory_restart: '500M',
      autorestart: true,
      time: true,
    },
  ],
};
