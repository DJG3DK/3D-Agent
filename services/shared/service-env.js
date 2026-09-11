'use strict';
/**
 * Where the two Node services read their own secrets.
 *
 * REVIEW_CONTROL_SECRET used to live in `services/llm-router/.env` for one
 * reason: that file already existed and both services already read it for the
 * OpenRouter key. So the model proxy became a secrets bus -- a file whose
 * blast radius is "anything that can read the router's config" ended up
 * holding the secret that authorises merge and deploy. Those are different
 * trust domains and they now have different files.
 *
 * Read order, first hit wins:
 *   1. process.env              -- an operator or pm2 passing it explicitly
 *   2. services/shared/.env     -- the home, written 0600 by install.sh
 *   3. services/llm-router/.env -- LEGACY. Deployments installed before
 *      2026-09-11 have it there and must keep working across an upgrade that
 *      does not re-run the installer. Logged once so it gets moved.
 *
 * Deliberately a hand-rolled reader, not dotenv: these services have one
 * dependency each on purpose, the format in question is `KEY=value` lines,
 * and process.env must win over a file rather than the other way round.
 */

const fs = require('fs');
const path = require('path');

const SHARED_ENV = 'services/shared/.env';
const LEGACY_ENV = 'services/llm-router/.env';

const warned = new Set();

function readFrom(file, name) {
    try {
        const m = fs.readFileSync(file, 'utf8').match(new RegExp(`^${name}=(.+)$`, 'm'));
        return m ? m[1].trim() : null;
    } catch {
        return null;
    }
}

/**
 * One secret, by name. `agentHome` is the installation root.
 * Returns the value or null; callers fail closed on null.
 */
function readServiceSecret(name, agentHome) {
    if (process.env[name]) return process.env[name].trim();

    const fromShared = readFrom(path.join(agentHome, SHARED_ENV), name);
    if (fromShared) return fromShared;

    const fromLegacy = readFrom(path.join(agentHome, LEGACY_ENV), name);
    if (fromLegacy) {
        if (!warned.has(name)) {
            warned.add(name);
            console.warn(
                `[service-env] ${name} was read from ${LEGACY_ENV}. Move it to ${SHARED_ENV} ` +
                `(mode 600): the model proxy's config should not carry service secrets.`,
            );
        }
        return fromLegacy;
    }
    return null;
}

module.exports = { readServiceSecret, SHARED_ENV, LEGACY_ENV };
