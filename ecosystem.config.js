// cwd is __dirname so this file works from any checkout location.
module.exports = {
  apps: [
    {
      name: '3d-agent',
      cwd: __dirname,
      script: '.venv/bin/uvicorn',
      args: 'agent.server:app --host 127.0.0.1 --port 8100',
      interpreter: 'none',
      // pm2 kills and restarts the process past this. It was set on the live
      // host with a CLI flag and lived only in pm2's own dump, which made it
      // invisible here: on 2026-09-12 a long task was killed four times in
      // eleven hours at a 1536 MiB cap, and the cause read as "it restarted
      // on its own" because nothing in the repo mentioned a cap at all.
      //
      // A restart is not free. It kills the in-flight work pass, which is
      // what lets the commit gate see anything -- so a cap low enough to trip
      // during a long pass means the task can never finish one, auto-resumes,
      // and starts over. The number has to be above what a real task needs,
      // not merely above idle: one pass holds a 70-80k-token message history,
      // a subagent's history beside it, and the checkpoint it is writing.
      //
      // 4 GiB on a 31 GiB box. Still a runaway catch, no longer a schedule.
      max_memory_restart: '4G',
      env: { PYTHONUNBUFFERED: '1' },
    },
  ],
};
