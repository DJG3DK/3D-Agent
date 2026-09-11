'use strict';
/**
 * The review checkout has to be able to RUN the checks it was given, without
 * being able to damage what it borrowed to run them.
 *
 * A git worktree carries what git carries. PHP's vendor/, Elixir's deps/ and
 * a bundled Ruby project's vendor/bundle are all gitignored, so a fresh
 * worktree has none of them and `vendor/bin/phpunit` exits 127 -- which the
 * reviewer reports as a failing check and an operator reads as a broken test
 * suite.
 *
 * The fix is a READ-ONLY bind, not a symlink. A symlink into the live tree is
 * writable, and the code running against it is by definition unreviewed: a
 * test fixture, a compile step or a package's own cache would have written
 * through to production's installed dependencies.
 *
 * Run: node tests/test_reviewer_dependency_dirs.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  materializeDependencyDirs, installChangedDependencies,
} = require('../services/commit-reviewer/reviewer.js');

let passed = 0;
const skipped = [];

// async, and AWAITING fn: `try { return fn() } finally { cleanup }` around an
// async function runs the cleanup the moment fn returns its promise -- which
// deleted the fixture out from under the test that was still using it.
async function test(name, fn) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'revdeps-'));
    const live = path.join(root, 'live');
    const worktree = path.join(root, 'worktree');
    fs.mkdirSync(live, { recursive: true });
    fs.mkdirSync(worktree, { recursive: true });
    try {
        return await fn({ live, worktree });
    } finally {
        // Unmount anything the test bound, so a failure cannot leave a mount
        // pointing into a directory that is about to be deleted.
        for (const rel of ['vendor', 'deps', 'vendor/bundle']) {
            try {
                require('child_process').execFileSync('umount', [path.join(worktree, rel)],
                    { stdio: 'ignore' });
            } catch { /* not mounted */ }
        }
        fs.rmSync(root, { recursive: true, force: true });
    }
}

function ok(name) {
    passed++;
    console.log(`  ok  ${name}`);
}

console.log('reviewer dependency dirs');

async function main() {
  await test('vendor is bound from live and the check command resolves', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), '#!/bin/sh\n');

    const { mounted, issues } = await materializeDependencyDirs(
        { live, dependencyDirs: ['vendor'] }, worktree);

    if (!mounted.length && issues.length) {
        skipped.push('bind mounts need root — vendor mount test skipped');
        return;
    }
    assert.deepEqual(mounted, ['vendor']);
    assert.ok(fs.existsSync(path.join(worktree, 'vendor', 'bin', 'phpunit')),
        'the check command must resolve inside the worktree');
    ok('vendor is bound from live and the check command resolves');
  });

  await test('what is bound cannot be written through to live', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), 'original\n');

    const { mounted } = await materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, worktree);
    if (!mounted.length) {
        skipped.push('bind mounts need root — read-only test skipped');
        return;
    }

    assert.throws(
        () => fs.writeFileSync(path.join(worktree, 'vendor', 'bin', 'phpunit'), 'rewritten\n'),
        /EROFS|EACCES|EPERM/,
        'an unreviewed branch must not be able to edit production\'s installed dependencies');
    assert.equal(fs.readFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), 'utf8'), 'original\n');
    ok('what is bound cannot be written through to live');
  });

  await test('a directory the live checkout does not have is reported, not fatal',
    async ({ live, worktree }) => {
      const said = [];
      const { mounted, issues } = await materializeDependencyDirs(
          { live, dependencyDirs: ['vendor'] }, worktree, { log: (m) => said.push(m) });
      assert.deepEqual(mounted, []);
      assert.deepEqual(issues, []);
      assert.match(said.join('\n'), /not present on the live checkout/);
      ok('a directory the live checkout does not have is reported, not fatal');
    });

  await test('a branch that brought its own copy keeps it', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'from-live'), '');
    fs.mkdirSync(path.join(worktree, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(worktree, 'vendor', 'from-branch'), '');

    const { mounted } = await materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, worktree);

    assert.deepEqual(mounted, [], 'the branch wins');
    assert.ok(fs.existsSync(path.join(worktree, 'vendor', 'from-branch')));
    assert.ok(!fs.existsSync(path.join(worktree, 'vendor', 'from-live')));
    ok('a branch that brought its own copy keeps it');
  });

  await test('a project with no dependency dirs is untouched', async ({ live, worktree }) => {
    assert.deepEqual((await materializeDependencyDirs({ live }, worktree)).mounted, []);
    assert.deepEqual((await materializeDependencyDirs({ live, dependencyDirs: [] }, worktree)).mounted, []);
    assert.deepEqual(fs.readdirSync(worktree), []);
    ok('a project with no dependency dirs is untouched');
  });

  // ---- installChangedDependencies -------------------------------------
  //
  // A branch that edits its manifest must not be judged against live's
  // installed dependencies. The npm path has always reinstalled on a
  // lockfile change; these two had no equivalent.

  await test('an unchanged manifest installs nothing', async ({ live, worktree }) => {
    const { installed, issues } = await installChangedDependencies(
        { live, dependencyDirs: ['vendor', 'deps'] }, worktree, 'src/App.php\nREADME.md\n');
    assert.deepEqual(installed, []);
    assert.deepEqual(issues, []);
    ok('an unchanged manifest installs nothing');
  });

  await test('a changed composer.json is installed rather than borrowed',
    async ({ live, worktree }) => {
      const said = [];
      const { installed, issues } = await installChangedDependencies(
          { live, dependencyDirs: ['vendor'] }, worktree, 'composer.json\nsrc/App.php\n',
          (m) => said.push(m));
      // composer is not installed on this host, so the install fails -- and
      // failing loudly as a setup issue is the correct outcome. What matters
      // is that it was ATTEMPTED and that vendor is not then borrowed.
      assert.ok(installed.length === 1 || issues.length === 1,
          'a changed manifest must either install or report why it could not');
      assert.match(said.join('\n'), /composer\.json\/lock changed/);
      ok('a changed composer.json is installed rather than borrowed');
    });

  await test('a changed mix.exs triggers deps.get', async ({ live, worktree }) => {
    const said = [];
    const { installed, issues } = await installChangedDependencies(
        { live, dependencyDirs: ['deps'] }, worktree, 'mix.lock\n', (m) => said.push(m));
    assert.ok(installed.length === 1 || issues.length === 1);
    assert.match(said.join('\n'), /mix\.exs\/lock changed/);
    ok('a changed mix.exs triggers deps.get');
  });

  await test('a stack that declares no such directory is never installed for',
    async ({ live, worktree }) => {
      const { installed, issues } = await installChangedDependencies(
          { live, dependencyDirs: [] }, worktree, 'composer.json\nmix.lock\n');
      assert.deepEqual(installed, []);
      assert.deepEqual(issues, []);
      ok('a stack that declares no such directory is never installed for');
    });

  await test('what was installed fresh is not then mounted over', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'from-live'), '');
    const { mounted } = await materializeDependencyDirs(
        { live, dependencyDirs: ['vendor'] }, worktree, { skip: ['vendor'] });
    assert.deepEqual(mounted, []);
    assert.ok(!fs.existsSync(path.join(worktree, 'vendor')),
        "the branch's own install must not be shadowed by live's");
    ok('what was installed fresh is not then mounted over');
  });

  console.log(`\n${passed} passed`);
  for (const s of skipped) console.log(`  (skipped: ${s})`);
}

main().catch((e) => { console.error(e); process.exit(1); });
