import calendar from './calendar.json' with { type: 'json' };

const REPO = 'ivssraviraj/alpaca-swing-trading';
const MINUTE = 60_000;
const DAY = 86_400_000;
const WINDOW = 3 * MINUTE;
const WORKFLOWS = { probe: 'scheduler-heartbeat.yml', observe: 'swing-scan.yml' };
const iso = (ms) => new Date(ms).toISOString();
const inNewYork = (value) => value ? new Date(value).toLocaleString('en-US', { timeZone: 'America/New_York' }) : null;
const key = (target, due) => `${target}:${due}`;
const json = (body, status = 200) => Response.json(body, { status });

export function settings(env) {
  if (!Object.hasOwn(WORKFLOWS, env.TARGET)) throw Error('Invalid target');
  const activeFrom = Date.parse(env.ACTIVE_FROM);
  if (!Number.isFinite(activeFrom)) throw Error('ACTIVE_FROM must be an ISO timestamp');
  return { target: env.TARGET, activeFrom };
}

export function expectedSlots(target, from, through) {
  const result = [];
  for (let day = Math.floor(from / DAY) * DAY; day <= through; day += DAY) {
    const date = iso(day).slice(0, 10);
    if (target === 'observe' && (date < calendar.valid_from || date > calendar.valid_through)) {
      throw Error('Exchange calendar expired or unavailable');
    }
    const bounds = target === 'probe' ? [day - 2 * MINUTE, day + DAY] : calendar.sessions[date];
    if (!bounds) continue;
    for (let due = bounds[0] + 3 * MINUTE; due < bounds[1]; due += 15 * MINUTE) {
      if (due >= from && due <= through) result.push({
        slot_key: key(target, due), target, due, deadline: Math.min(due + WINDOW, bounds[1]),
      });
    }
  }
  return result;
}

export function eligible(slot, nominal, now) {
  // A delayed old Cron event cannot be relabelled as the current slot.
  return slot.due <= nominal && nominal < slot.deadline &&
    slot.due <= now && now < slot.deadline;
}

export function classification(row, now) {
  const receipt = row.receipt && JSON.parse(row.receipt);
  if (receipt?.status === 'COMPLETED') {
    if (receipt.engine_status === 'STALE_SLOT') return 'EXPIRED_BEFORE_ENGINE';
    return receipt.scanned === 0 ? 'BOT_GATE_OR_EMPTY_SCAN' : 'ENGINE_COMPLETED';
  }
  if (receipt) return row.conclusion && row.conclusion !== 'success' ? 'ENGINE_FAILED' : 'ENGINE_STARTED';
  if (!row.run_id) return now >= row.deadline ? 'MISSING_RUN_RECORD' : 'AWAITING_RUN';
  if (row.conclusion && row.conclusion !== 'success') return 'WORKFLOW_FAILED';
  if (!row.job_started_at) return now >= row.deadline ? 'QUEUE_DEADLINE_MISSED' : 'QUEUED';
  if (row.target === 'observe' && Date.parse(row.job_started_at) >= row.deadline) return 'QUEUE_DEADLINE_MISSED';
  if (row.target === 'probe') return row.conclusion === 'success' ? 'PROBE_SUCCESS' : 'PROBE_STARTED';
  return row.conclusion ? 'NO_ENGINE_RECEIPT' : 'JOB_STARTED';
}

async function github(env, path, body) {
  const response = await fetch(`https://api.github.com/repos/${REPO}/${path}`, {
    method: body ? 'POST' : 'GET',
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'alpaca-swing-clock',
      'Content-Type': 'application/json',
      'X-GitHub-Api-Version': '2026-03-10',
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
    redirect: 'error', signal: AbortSignal.timeout(15_000),
  });
  if (response.status !== 200) throw Error(`GITHUB_HTTP_${response.status}`);
  return response.json();
}

async function rememberExpected(env, slots) {
  if (!slots.length) return;
  await env.DB.prepare(`INSERT OR IGNORE INTO slots(slot_key, target, due, deadline)
    SELECT json_extract(value,'$.slot_key'), json_extract(value,'$.target'),
    json_extract(value,'$.due'), json_extract(value,'$.deadline') FROM json_each(?1)`)
    .bind(JSON.stringify(slots)).run();
}

async function dispatch(env, slot, nominal, now) {
  if (!eligible(slot, nominal, now)) return;
  // Atomic admission across concurrent cron deliveries; reserve before the HTTP call.
  const claim = await env.DB.prepare(`UPDATE slots SET attempts=attempts+1, next_attempt=?1
    WHERE slot_key=?2 AND run_id IS NULL AND receipt IS NULL
      AND attempts<2 AND next_attempt<=?3 AND deadline>?3`)
    .bind(now + MINUTE, slot.slot_key, now).run();
  if (claim.meta.changes !== 1) return;
  try {
    const inputs = slot.target === 'observe' ? { mode: 'observe', scheduled_for: iso(slot.due) } : {};
    const result = await github(env, `actions/workflows/${WORKFLOWS[slot.target]}/dispatches`,
      { ref: 'main', inputs });
    if (!result.workflow_run_id) throw Error('MISSING_DISPATCH_RUN_ID');
    await env.DB.prepare('UPDATE slots SET run_id=?1, last_error=NULL WHERE slot_key=?2')
      .bind(String(result.workflow_run_id), slot.slot_key).run();
    console.log(JSON.stringify({ event: 'dispatched', slot: slot.slot_key, run_id: result.workflow_run_id }));
  } catch (error) {
    // Do not log request headers, response bodies, credentials or arbitrary provider errors.
    const code = /^GITHUB_HTTP_\d+$/.test(error.message) ? error.message : 'DISPATCH_UNCONFIRMED';
    await env.DB.prepare('UPDATE slots SET last_error=?1 WHERE slot_key=?2').bind(code, slot.slot_key).run();
  }
}

async function reconcile(env, now) {
  const { results } = await env.DB.prepare(`SELECT * FROM slots
    WHERE due>?1 AND run_id IS NOT NULL AND conclusion IS NULL AND last_checked<?2
    ORDER BY due DESC LIMIT 4`).bind(now - DAY, now - 55_000).all();
  for (const row of results) {
    const receipt = row.receipt && JSON.parse(row.receipt);
    const runId = receipt?.github_run_id || row.run_id;
    try {
      const run = await github(env, `actions/runs/${runId}`);
      const jobs = await github(env, `actions/runs/${runId}/jobs?per_page=100`);
      const started = jobs.jobs.find((job) => job.started_at)?.started_at || null;
      await env.DB.prepare(`UPDATE slots SET run_created_at=?1, job_started_at=?2,
        conclusion=?3, last_checked=?4, last_error=NULL WHERE slot_key=?5`)
        .bind(run.created_at, started, run.conclusion, now, row.slot_key).run();
    } catch {
      await env.DB.prepare('UPDATE slots SET last_error=?1, last_checked=?2 WHERE slot_key=?3')
        .bind('GITHUB_STATUS_UNAVAILABLE', now, row.slot_key).run();
    }
  }
}

export async function tick(controller, env, now = Date.now()) {
  if (env.ENABLED !== 'true') return;
  const { target, activeFrom } = settings(env);
  // Restore expected records after an outage, but never replay expired work.
  const expected = expectedSlots(target, Math.max(activeFrom, now - DAY), now);
  await rememberExpected(env, expected);
  for (const slot of expected.filter((s) => eligible(s, controller.scheduledTime, now))) {
    await dispatch(env, slot, controller.scheduledTime, Date.now());
  }
  await reconcile(env, now);
  await env.DB.prepare(`INSERT INTO health(id,last_tick) VALUES(1,?1)
    ON CONFLICT(id) DO UPDATE SET last_tick=MAX(last_tick,excluded.last_tick)`).bind(now).run();
}

async function authorized(request, token) {
  if (!token) return false;
  const encoder = new TextEncoder();
  const hashes = await Promise.all([request.headers.get('Authorization') || '', `Bearer ${token}`]
    .map((value) => crypto.subtle.digest('SHA-256', encoder.encode(value))));
  const a = new Uint8Array(hashes[0]), b = new Uint8Array(hashes[1]);
  return a.reduce((diff, value, i) => diff | (value ^ b[i]), 0) === 0;
}

export async function handleRequest(request, env, now = Date.now()) {
  const path = new URL(request.url).pathname;
  if (request.method === 'GET' && path === '/health') {
    const row = await env.DB.prepare('SELECT last_tick FROM health WHERE id=1').first();
    const recent = await env.DB.prepare('SELECT * FROM slots WHERE due>=?1 AND deadline<=?2')
      .bind(now - 30 * MINUTE, now).all();
    const faults = new Set(['MISSING_RUN_RECORD', 'QUEUE_DEADLINE_MISSED', 'EXPIRED_BEFORE_ENGINE',
      'ENGINE_FAILED', 'WORKFLOW_FAILED', 'NO_ENGINE_RECEIPT']);
    const faultCount = recent.results.filter((slot) => faults.has(classification(slot, now))).length;
    const healthy = env.ENABLED === 'true' && row && now - row.last_tick < 3 * MINUTE && !faultCount;
    return json({ healthy: Boolean(healthy), last_tick: row ? iso(row.last_tick) : null,
      recent_failed_slots: faultCount }, healthy ? 200 : 503);
  }
  if (request.method === 'GET' && path === '/status' && await authorized(request, env.MONITOR_TOKEN)) {
    const rows = await env.DB.prepare('SELECT * FROM slots WHERE due>=?1 ORDER BY due DESC')
      .bind(now - DAY).all();
    const { target, activeFrom } = settings(env);
    const existing = new Set(rows.results.map((row) => row.slot_key));
    const missing = expectedSlots(target, Math.max(activeFrom, now - DAY), now)
      .filter((slot) => !existing.has(slot.slot_key));
    return json({ slots: [...rows.results, ...missing].sort((a, b) => b.due - a.due).map((row) => ({ ...row,
      status: classification(row, now), clock_record_missing: !existing.has(row.slot_key),
      due_utc: iso(row.due), due_new_york: inNewYork(row.due),
      run_created_at_new_york: inNewYork(row.run_created_at),
      job_started_at_new_york: inNewYork(row.job_started_at),
      creation_to_job_seconds: row.run_created_at && row.job_started_at ?
        (Date.parse(row.job_started_at) - Date.parse(row.run_created_at)) / 1000 : null,
      run_url: (row.receipt ? JSON.parse(row.receipt).github_run_id : row.run_id) ?
        `https://github.com/${REPO}/actions/runs/${row.receipt ? JSON.parse(row.receipt).github_run_id : row.run_id}` : null,
    })) });
  }
  if (request.method === 'POST' && path === '/receipt' && await authorized(request, env.RECEIPT_TOKEN)) {
    const raw = await request.text();
    if (raw.length > 4096) return json({ error: 'too_large' }, 413);
    let receipt;
    try { receipt = JSON.parse(raw); } catch { return json({ error: 'invalid_json' }, 400); }
    const due = Date.parse(receipt.scheduled_for);
    if (!Number.isFinite(due) || !/^\d+$/.test(String(receipt.github_run_id)) ||
      !['STARTED', 'COMPLETED'].includes(receipt.status)) return json({ error: 'invalid_receipt' }, 400);
    // The execution owner can differ from the last dispatch if an HTTP response was lost.
    // Never replace a completed receipt with a delayed STARTED callback.
    const result = await env.DB.prepare(`UPDATE slots SET receipt=?1,
      run_id=COALESCE(run_id,?2), conclusion=NULL, last_checked=0
      WHERE slot_key=?3 AND (receipt IS NULL OR
        (json_extract(receipt,'$.status')!='COMPLETED' AND
         json_extract(receipt,'$.github_run_id')=?2))`)
      .bind(raw, String(receipt.github_run_id), key('observe', due)).run();
    if (result.meta.changes === 0) {
      const existing = await env.DB.prepare('SELECT receipt FROM slots WHERE slot_key=?1')
        .bind(key('observe', due)).first();
      if (!existing) return json({ error: 'unknown_slot' }, 404);
    }
    return json({ accepted: true });
  }
  return json({ error: 'not_found' }, 404);
}

export default {
  scheduled(controller, env) { return tick(controller, env); },
  fetch(request, env) { return handleRequest(request, env); },
};
