const fs = require('fs');
const path = require('path');

// The passkey gate needs only its own GATE_* settings, which live beside the
// router's secrets in .env (gitignored). Lift just those, so the gate process
// never holds the OpenRouter or master key it has no use for.
function gateEnv() {
    const out = {};
    try {
        for (const line of fs.readFileSync(path.join(__dirname, '.env'), 'utf8').split('\n')) {
            const m = /^\s*(GATE_[A-Z_]+)\s*=\s*(.*?)\s*$/.exec(line);
            if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '');
        }
    } catch { /* no .env yet: the gate exits with a clear message */ }
    return out;
}

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
        {
            // WebAuthn passkey gate in front of the public admin panel. nginx
            // consults its /auth/check via auth_request; see auth-gate/server.mjs.
            name:          'llm-auth-gate',
            script:        'server.mjs',
            cwd:           path.join(__dirname, 'auth-gate'),
            restart_delay: 3000,
            env:           gateEnv(),
        },
    ],
};
