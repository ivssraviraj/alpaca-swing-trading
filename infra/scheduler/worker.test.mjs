import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { expectedSlots, eligible, classification, handleRequest, tick } from './worker.mjs';

const time = (s) => Date.parse(s);
const originalFetch = globalThis.fetch, originalNow = Date.now;
let db, env, now, posts;

// Exercise the actual SQL and uniqueness constraints without a Cloudflare account.
function statement(sql, args = []) {
  const stmt = db.prepare(sql);
  return {
    bind(...bound) { return statement(sql, bound); },
    async run() { return { meta: { changes: stmt.run(...args).changes } }; },
    async all() { return { results: stmt.all(...args) }; },
    async first() { return stmt.get(...args) || null; },
  };
}

beforeEach(() => {
  db = new DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('./schema.sql', import.meta.url), 'utf8'));
  now = time('2026-11-30T14:33:10Z');
  Date.now = () => now;
  env = { ENABLED: 'true', TARGET: 'observe', ACTIVE_FROM: '2026-11-30T14:00:00Z',
    GITHUB_TOKEN: 'fixture', RECEIPT_TOKEN: 'receipt-fixture', MONITOR_TOKEN: 'monitor-fixture',
    DB: { prepare: statement } };
  posts = [];
  globalThis.fetch = async (url, init) => {
    assert.match(url, /^https:\/\/api.github.com\/repos\/ivssraviraj\/alpaca-swing-trading\//);
    if (init.method === 'POST') {
      posts.push(JSON.parse(init.body));
      return Response.json({ workflow_run_id: 123 });
    }
    return Response.json(url.includes('/jobs?') ? { jobs: [{ started_at: '2026-11-30T14:33:20Z' }] } :
      { created_at: '2026-11-30T14:33:12Z', conclusion: null });
  };
});
afterEach(() => { db.close(); globalThis.fetch = originalFetch; Date.now = originalNow; });

function slots(day) {
  return expectedSlots('observe', time(`${day}T00:00:00Z`), time(`${day}T23:59:59Z`));
}

test('NYSE holidays, early close, DST and actual month counts', () => {
  assert.equal(slots('2026-11-23').length, 26);
  assert.equal(slots('2026-11-27').length, 14);
  assert.equal(slots('2026-11-26').length, 0);
  assert.equal(slots('2026-11-28').length, 0);
  assert.equal(new Date(slots('2026-03-06')[0].due).toISOString(), '2026-03-06T14:33:00.000Z');
  assert.equal(new Date(slots('2026-03-09')[0].due).toISOString(), '2026-03-09T13:33:00.000Z');
  assert.equal(new Date(slots('2026-11-02')[0].due).toISOString(), '2026-11-02T14:33:00.000Z');
  assert.equal(expectedSlots('observe', time('2026-11-01'), time('2026-12-01') - 1).length, 508);
  assert.equal(expectedSlots('observe', time('2026-12-01'), time('2027-01-01') - 1).length, 560);
  assert.throws(() => slots('2028-01-03'), /calendar/);
});

test('weekend probe has 96 slots and old ticks cannot masquerade as new slots', () => {
  const probe = expectedSlots('probe', time('2026-11-28'), time('2026-11-29') - 1);
  assert.equal(probe.length, 96);
  assert.equal(eligible(probe[1], probe[0].due, probe[1].due), false);
  assert.equal(eligible(probe[0], probe[0].due, probe[0].deadline), false);
});

test('concurrent deliveries and sequential redelivery dispatch once', async () => {
  const event = { scheduledTime: time('2026-11-30T14:33:00Z') };
  await Promise.all([tick(event, env), tick(event, env)]);
  await tick(event, env);
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0], { ref: 'main', inputs: { mode: 'observe', scheduled_for: '2026-11-30T14:33:00.000Z' } });
  const row = db.prepare('SELECT * FROM slots').get();
  assert.equal(row.run_id, '123');
  assert.equal(row.attempts, 1);
  assert.equal(row.job_started_at, '2026-11-30T14:33:20Z');
});

test('uncertain delivery retries original timestamp only once within deadline', async () => {
  globalThis.fetch = async (_url, init) => {
    if (init.method === 'POST') posts.push(JSON.parse(init.body));
    throw Error('Network unavailable');
  };
  await tick({ scheduledTime: now - 10_000 }, env);
  now += 60_000;
  await tick({ scheduledTime: now - 10_000 }, env);
  now += 60_000;
  await tick({ scheduledTime: now - 10_000 }, env);
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[0], posts[1]);
  now += 60_000;
  await tick({ scheduledTime: now - 10_000 }, env);
  assert.equal(posts.length, 2);
  assert.equal(classification(db.prepare('SELECT * FROM slots').get(), now), 'MISSING_RUN_RECORD');
});

test('recovery records missed slots without replaying them', async () => {
  now = time('2026-11-30T15:21:00Z');
  await tick({ scheduledTime: now }, env);
  assert.equal(posts.length, 0);
  assert.equal(db.prepare('SELECT count(*) AS n FROM slots').get().n, 4);
  assert.equal(classification(db.prepare('SELECT * FROM slots LIMIT 1').get(), now), 'MISSING_RUN_RECORD');
});

test('disabled setup makes no dispatch or database changes', async () => {
  await tick({ scheduledTime: now }, { ...env, ENABLED: 'false' });
  assert.equal(posts.length, 0);
  assert.equal(db.prepare('SELECT count(*) AS n FROM slots').get().n, 0);
});

test('authenticated completion is monotonic and reports bot gate distinctly', async () => {
  await tick({ scheduledTime: now - 10_000 }, env);
  const receipt = { scheduled_for: '2026-11-30T14:33:00+00:00', github_run_id: '123',
    status: 'COMPLETED', engine_status: 'ENTRY_FREEZE', scanned: 0 };
  function request(body, token = 'receipt-fixture') {
    return new Request('https://fixture.workers.dev/receipt', { method: 'POST',
      headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(body) });
  }
  assert.equal((await handleRequest(request(receipt, 'wrong'), env)).status, 404);
  assert.equal((await handleRequest(request(receipt), env)).status, 200);
  assert.equal((await handleRequest(request({ ...receipt, status: 'STARTED' }), env)).status, 200);
  const row = db.prepare('SELECT * FROM slots').get();
  assert.equal(classification(row, now), 'BOT_GATE_OR_EMPTY_SCAN');
  assert.equal(JSON.parse(row.receipt).engine_status, 'ENTRY_FREEZE');
});

test('health exposes clock failure and status requires separate credential', async () => {
  await tick({ scheduledTime: now - 10_000 }, env);
  assert.equal((await handleRequest(new Request('https://fixture/health'), env)).status, 200);
  now += 3 * 60_000;
  assert.equal((await handleRequest(new Request('https://fixture/health'), env)).status, 503);
  assert.equal((await handleRequest(new Request('https://fixture/status'), env)).status, 404);
});
