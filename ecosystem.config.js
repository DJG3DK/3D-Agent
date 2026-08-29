// cwd is __dirname so this file works from any checkout location.
module.exports = {
  apps: [
    {
      name: '3d-agent',
      cwd: __dirname,
      script: '.venv/bin/uvicorn',
      args: 'agent.server:app --host 127.0.0.1 --port 8100',
      interpreter: 'none',
      env: { PYTHONUNBUFFERED: '1' },
    },
  ],
};
