'use strict';
/**
 * One source of truth for which projects exist, shared by the reviewer and
 * the deploy service.
 *
 * Before this, three files each carried their own project map: the agent's
 * projects.json (paths), commit-reviewer/reviewer.js (check commands, secret
 * files, mounts) and agent-review/server.js (build steps, pm2 apps). Adding a
 * project meant hand-editing all three, and a project added to one but not
 * the others silently half-worked -- the agent could build in it while the
 * reviewer ignored its commits.
 *
 * The merge rule is deliberately "built-in wins":
 *
 *   final = { ...fromProjectsJson, ...builtinOverride }
 *
 * The three original projects carry hand-tuned configs that encode real
 * incidents -- a test suite that POSTed live trade orders, a Prisma client
 * that had to be regenerated before a build could typecheck, a storefront
 * prerender step whose absence 404'd a production catalog. None of that is
 * derivable, so the built-in entries stay authoritative and are never
 * overwritten by generated config. Projects onboarded through the wizard
 * have no built-in entry, so they run entirely on projects.json.
 *
 * Read fresh on each call (cheap, small file) so a project onboarded at
 * runtime is picked up without restarting these services.
 */

const fs = require('fs');
const path = require('path');

const PROJECTS_JSON = process.env.AGENT_PROJECTS_JSON
    || path.join(__dirname, '..', '..', 'projects.json');

function readProjectsJson(file = PROJECTS_JSON) {
    try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
        return (parsed && parsed.projects) || {};
    } catch (err) {
        // A missing or malformed projects.json must not take down a running
        // reviewer -- it falls back to the built-in map and says so.
        if (err.code !== 'ENOENT') {
            console.error(`[projects-config] ${file} unreadable (${err.message}) — using built-ins only`);
        }
        return {};
    }
}

/**
 * @param {object} builtins  the service's own hand-tuned map (authoritative)
 * @param {object} [opts]
 * @param {'review'|'deploy'} opts.section  which sub-object of a projects.json
 *        entry carries this service's fields
 * @param {string} [opts.file]
 */
function loadProjects(builtins, { section, file } = {}) {
    const fromJson = readProjectsJson(file);
    const out = {};
    for (const [name, entry] of Object.entries(fromJson)) {
        const base = {
            live: entry.live,
            // projects.json calls the workspace "sandbox" (its original name);
            // the deploy service calls the same directory "workspace".
            sandbox: entry.sandbox,
            workspace: entry.sandbox,
        };
        const extra = (section && entry[section]) || {};
        out[name] = { ...base, ...extra };
    }
    // Built-ins win, and a built-in-only project (one deliberately not in
    // projects.json) still appears.
    for (const [name, cfg] of Object.entries(builtins || {})) {
        out[name] = { ...(out[name] || {}), ...cfg };
    }
    return out;
}

module.exports = { loadProjects, readProjectsJson, PROJECTS_JSON };
