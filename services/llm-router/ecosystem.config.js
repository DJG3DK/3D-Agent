module.exports = {
    apps: [
        {
            name:          'llm-router',
            script:        'venv/bin/litellm',
            // 0.0.0.0 so the OpenHands Docker container can reach it via the
            // docker0 bridge (host.docker.internal) — NOT publicly exposed:
            // UFW default-denies incoming and only explicitly allows the
            // docker bridge subnet to reach this port (see server firewall
            // rules). The proxy's own LITELLM_MASTER_KEY auth is a second
            // layer regardless.
            args:          '--config config.yaml --port 4000 --host 0.0.0.0',
            cwd:           __dirname,
            interpreter:   'none',   // it's already a venv-shebang'd executable, not a .js file
            restart_delay: 3000,
            env: {
                // litellm shells out to the `prisma` CLI on startup to check
                // migrations against the database. Without the venv on PATH it
                // cannot find it and the proxy EXITS during startup rather than
                // degrading — found by rehearsing this on a spare port first.
                PATH: `${__dirname}/venv/bin:${process.env.PATH}`,
            },
        },
    ],
};
