// Where the Node services read REVIEW_CONTROL_SECRET from, and in what order.
// The router's .env used to be the only home, which made the model proxy a
// secrets bus; services/shared/.env is the home now, with the old path kept
// as a fallback so an upgrade that does not re-run the installer still boots.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { readServiceSecret, SHARED_ENV, LEGACY_ENV } = require('../services/shared/service-env.js');

function home(files) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'svc-env-'));
  for (const [rel, body] of Object.entries(files)) {
    fs.mkdirSync(path.join(root, path.dirname(rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), body);
  }
  return root;
}

test('the shared env file is the home', () => {
  const root = home({ [SHARED_ENV]: 'REVIEW_CONTROL_SECRET=from-shared\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-shared');
});

test('the router env still works for a deployment installed before the split', () => {
  const root = home({ [LEGACY_ENV]: 'OPENROUTER_API_KEY=k\nREVIEW_CONTROL_SECRET=from-legacy\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-legacy');
});

test('the shared file wins over the legacy one', () => {
  const root = home({
    [SHARED_ENV]: 'REVIEW_CONTROL_SECRET=new\n',
    [LEGACY_ENV]: 'REVIEW_CONTROL_SECRET=old\n',
  });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'new');
});

test('the environment wins over every file', () => {
  const root = home({ [SHARED_ENV]: 'REVIEW_CONTROL_SECRET=file\n' });
  process.env.REVIEW_CONTROL_SECRET = 'from-env';
  try {
    assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-env');
  } finally {
    delete process.env.REVIEW_CONTROL_SECRET;
  }
});

test('a missing secret is null, so the caller can fail closed', () => {
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', home({})), null);
  assert.equal(readServiceSecret('NOPE', home({ [SHARED_ENV]: 'OTHER=1\n' })), null);
});

test('a value is not confused with a similarly named key', () => {
  const root = home({ [SHARED_ENV]: 'NOT_REVIEW_CONTROL_SECRET=wrong\nREVIEW_CONTROL_SECRET=right\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'right');
});
