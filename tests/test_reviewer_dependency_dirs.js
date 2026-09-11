'use strict';
/**
 * The review checkout has to be able to RUN the checks it was given.
 *
 * A git worktree carries what git carries. PHP's vendor/ and Elixir's deps/
 * are gitignored, so `vendor/bin/phpunit` in a fresh worktree exits 127 --
 * which the reviewer reports as a failing check, and an operator reads as a
 * broken test suite. node_modules had exactly this problem and got a
 * symlink; these stacks are first-class now and need the same.
 *
 * Run: node tests/test_reviewer_dependency_dirs.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { materializeDependencyDirs } = require('../services/commit-reviewer/reviewer.js');

let passed = 0;
function test(name, fn) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'revdeps-'));
    const live = path.join(root, 'live');
    const worktree = path.join(root, 'worktree');
    fs.mkdirSync(live, { recursive: true });
    fs.mkdirSync(worktree, { recursive: true });
    try {
        fn({ live, worktree });
        passed++;
        console.log(`  ok  ${name}`);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
}

console.log('reviewer dependency dirs');

test('vendor is linked from live, and resolves to the real binary', ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), '#!/bin/sh\n');

    const linked = materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, worktree);

    assert.deepEqual(linked, ['vendor']);
    assert.ok(fs.existsSync(path.join(worktree, 'vendor', 'bin', 'phpunit')),
        'the check command must resolve inside the worktree');
    assert.ok(fs.lstatSync(path.join(worktree, 'vendor')).isSymbolicLink(),
        'a symlink, not a copy: an install here would run untrusted scripts');
});

test('elixir gets both deps and _build', ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'deps', 'jason'), { recursive: true });
    fs.mkdirSync(path.join(live, '_build', 'test'), { recursive: true });
    const linked = materializeDependencyDirs({ live, dependencyDirs: ['deps', '_build'] }, worktree);
    assert.deepEqual(linked, ['deps', '_build']);
});

test('a directory the live checkout does not have is reported, not fatal', ({ live, worktree }) => {
    const said = [];
    const linked = materializeDependencyDirs(
        { live, dependencyDirs: ['vendor'] }, worktree, (m) => said.push(m));
    assert.deepEqual(linked, []);
    assert.match(said.join('\n'), /not present on the live checkout/);
    assert.ok(!fs.existsSync(path.join(worktree, 'vendor')));
});

test("a branch that committed its own copy keeps it", ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'from-live'), '');
    fs.mkdirSync(path.join(worktree, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(worktree, 'vendor', 'from-branch'), '');

    const linked = materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, worktree);

    assert.deepEqual(linked, [], 'the branch wins');
    assert.ok(fs.existsSync(path.join(worktree, 'vendor', 'from-branch')));
    assert.ok(!fs.existsSync(path.join(worktree, 'vendor', 'from-live')));
});

test('a project with no dependency dirs is untouched', ({ live, worktree }) => {
    assert.deepEqual(materializeDependencyDirs({ live }, worktree), []);
    assert.deepEqual(materializeDependencyDirs({ live, dependencyDirs: [] }, worktree), []);
    assert.deepEqual(fs.readdirSync(worktree), []);
});

test('a nested path is created rather than skipped', ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'apps', 'api', 'vendor'), { recursive: true });
    const linked = materializeDependencyDirs({ live, dependencyDirs: ['apps/api/vendor'] }, worktree);
    assert.deepEqual(linked, ['apps/api/vendor']);
    assert.ok(fs.existsSync(path.join(worktree, 'apps', 'api', 'vendor')));
});

console.log(`\n${passed} passed`);
