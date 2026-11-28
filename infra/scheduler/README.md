# Independent clock: prepared, not activated

**Deferred on 2026-11-28:** the user chose to diagnose and use native GitHub
scheduling without Cloudflare. No Cloudflare account, deployment, secrets or
external authority were activated. The following is an inactive design reference.

GitHub Actions does **not** need a first manual run to enable cron. `workflow_dispatch`
starts one run. Cloudflare is optional: this design replaces the source of timed
triggers while the existing GitHub workflow remains the executor. It cannot remove
GitHub runner queues or guarantee execution at an exact wall-clock time.

The deployed diagnostic heartbeat has run successfully on push and manual dispatch.
The manual test created run [46104816332](https://github.com/ivssraviraj/alpaca-swing-trading/actions/runs/46104816332)
at 2026-11-27 22:51:19 EST. As of 23:06 EST there was still no `schedule` run record,
including for the 23:01 slot. This does not identify GitHub's internal failure cause
or prove the event can never arrive later.

Follow-up at 2026-11-28 12:47 EST found two automatic heartbeat runs:
[46106820337](https://github.com/ivssraviraj/alpaca-swing-trading/actions/runs/46106820337)
created Friday 23:17:10 EST, and
[46149683504](https://github.com/ivssraviraj/alpaca-swing-trading/actions/runs/46149683504)
created Saturday 08:27:12 EST. The 9-hour-10-minute creation gap is incompatible
with reliable 15-minute delivery. Their original intended slots are not exposed,
and the first occurring after the manual run does not establish causation.

## Why this option

| Option | What it addresses | Limitation |
|---|---|---|
| Correct native cron | Wrong hours / timezone | Existing UTC envelope already covers both DST states; no missing-hour defect established |
| Second GitHub workflow | Isolates engine, permissions, concurrency and environment | Shares GitHub's scheduled-event service; our independent heartbeat also has a nine-hour gap |
| External clock + dispatch | Removes dependence on GitHub's scheduled-event delivery | Still depends on GitHub API/runners; requires account, narrow credential, monitoring |

Recommend the external clock after the non-trading verification below. There is no
claim that Cloudflare itself is required, infallible, or already deployed.

## Actual invocation counts and limits

All figures are planned invocations, before provider failures or retries.

| Work | November 2026 | December 2026 |
|---|---:|---:|
| Existing weekday UTC envelope, plus weekly report | 844 | 924 |
| New native heartbeat, 24 hours / all days | 2,880 | 2,976 |
| External per-minute clock ticks | 43,200 | 44,640 |
| Regular-session observe dispatches | 508 | 560 |
| Engine receipts, two per successful slot | 1,016 | 1,120 |

NYSE full sessions have 26 slots, early closes 14, at 09:33, 09:48 ... before close
in New York. Weekends/holidays have zero market dispatches. The calendar includes
Thanksgiving, the following early close, Christmas and the December 24 early close,
and converts DST into UTC session timestamps. `npm test` checks the monthly totals.
Probe mode instead dispatches 96 times/day, including weekends. Stop the probe after
verification to avoid unnecessary GitHub jobs.

Workers Free allows 100,000 requests/day and 10 ms CPU/invocation. This clock has
1,440 scheduled invocations/day, plus callbacks and any status polling. Network wait
does not equal CPU time; verify deployed CPU metrics before calling this validated
on Free. D1 Free allows 5 million rows read/day, 100,000 rows written/day and 5 GB.
The ledger is small; inspect actual D1 row metrics after deployment. No paid plan,
domain, broker key or AI key is required. Keep the account on Workers Free. Setup
requires account signup, a D1 database, a repo-scoped credential, and a test cycle;
allow roughly 30–60 minutes once the account is available, plus cron propagation
and observation. Do not upgrade automatically if a free limit is insufficient.

## Admission and failure behavior

- Worker is disabled by default; target is the secret-free heartbeat. The only
  engine mode implemented in the Worker is `observe`.
- D1 records expected slots before dispatch, reserves each attempt atomically and
  stores the returned GitHub run ID. At most two dispatch attempts are allowed,
  always with the original `scheduled_for`, within a three-minute admission window.
- The engine uses an atomic, durable PostgreSQL `kv` claim before entering its
  pipeline. The key normalizes UTC and excludes mode, source, retry and run ID.
  A completed or crashed claim remains claimed. This guarantees **at most one
  admitted execution**, not exactly one successful completion.
- The existing GitHub concurrency group and PostgreSQL advisory lock only prevent
  overlap. They do not prevent a sequential duplicate after the first run finishes.
- On explicit external cutover, native scheduled scans return
  `NATIVE_SCHEDULE_DISABLED` before broker calls. Native events lack a trustworthy
  original slot timestamp; assigning a delayed one to the current slot is unsafe.
  Weekly reports remain separate. Manual scans must provide a valid original slot
  after cutover. Default native behavior remains unchanged until that cutover.
- Both admission and the final pipeline start check reject expired slots. Old
  events and retries are never advanced to a newer slot or replayed for trading.
  A run may be created but wait too long for a runner, dependencies or the lock;
  it then cannot enter the engine for that slot.
- Receipts expose the actual engine status and scanned count, including gates such
  as `ENTRY_FREEZE`; a zero scan alone is not proof of a particular gate.
- Recovery reconstructs the past 24 hours of expected records without replay.
  `/status` also computes missing expectations if the clock itself stopped.

## Prepare a new free account

1. Create and verify a [Cloudflare account](https://dash.cloudflare.com/sign-up).
   Select Workers Free. Signup/sign-in is a user step; do not paste credentials here.
2. From this directory run `npm ci`, then `npx wrangler login`. The browser OAuth
   login is local to your machine. No account or login has been created by this change.
3. Run `npx wrangler d1 create alpaca-swing-clock`. Copy `wrangler.jsonc` to the
   ignored `wrangler.local.jsonc`, put the returned database ID there, and leave
   `ENABLED` as `false` and `TARGET` as `probe`.
4. Initialize the free ledger:

   ```sh
   npx wrangler d1 execute alpaca-swing-clock --remote --file schema.sql --config wrangler.local.jsonc
   ```

5. Create a fine-grained GitHub PAT for **only** `ivssraviraj/alpaca-swing-trading`,
   with **Actions: read and write** (Metadata read is implicit), a short expiration,
   and a reminder to rotate it. No Contents write or repository administration is
   needed. Actions permission is repository-wide: it cannot be restricted to one
   workflow or to observe mode. A GitHub App installation token is an alternative
   with automatic short-lived tokens, but requires more setup. Do not reuse/export
   the broad `gh` login token.
6. Store the PAT via the local prompt, not in source/chat:

   ```sh
   npx wrangler secret put GITHUB_TOKEN --config wrangler.local.jsonc
   npx wrangler secret put MONITOR_TOKEN --config wrangler.local.jsonc
   npx wrangler secret put RECEIPT_TOKEN --config wrangler.local.jsonc
   ```

   Generate two different random tokens for monitoring and receipts. Later store
   the same receipt token as GitHub environment `paper` secret
   `SCHEDULER_RECEIPT_TOKEN`. Never place broker/AI credentials in Cloudflare.
7. Deploy disabled with `npx wrangler deploy --config wrangler.local.jsonc`. This
   uses the Free plan; stop if the dashboard asks to activate a paid service.

## Verify in stages

1. **Offline first:** `npm test`, `npm run check`, and the repository Python checks.
   These use fixtures; no workflow dispatch or trade occurs. The calendar can be
   regenerated with the repo environment's `python infra/scheduler/calendar_export.py`.
   Regenerate/review it before expiry (2027-12-31) or an unscheduled exchange closure.
2. **Weekend clock probe:** in the local Wrangler config set `TARGET=probe`,
   `ACTIVE_FROM` to a future UTC time and `ENABLED=true`; redeploy. Allow up to 15
   minutes for cron propagation. Confirm two consecutive quarter-hour slots in
   authenticated `/status`: slot UTC/NY → dispatch run ID → GitHub creation time →
   job start → `PROBE_SUCCESS`. The GitHub event is `workflow_dispatch`, because
   the scheduler is external. Only `schedule` proves the separate native cron.
3. **Observe cutover:** deploy the engine guard changes to `main` first, and wait for
   existing scan executions to finish. Set the GitHub environment `paper` variables
   `SCHEDULER_AUTHORITY=external`, `EXTERNAL_SCHEDULER_MODE=observe`, and
   `SCHEDULER_RECEIPT_URL=https://<your-worker>/receipt`; install the receipt secret.
   Switch Worker `TARGET=observe`, use a fresh future `ACTIVE_FROM`, and redeploy.
   No paper execution is enabled by this procedure. Normal default-branch CI must pass.
4. **Market-session evidence:** confirm a slot's returned run ID, exact matching
   input, run creation, job start and engine STARTED/COMPLETED receipt. Download
   `schedule_receipt.json` from that run's audit artifact and compare the persistent
   PostgreSQL claim. Verify observe output has zero submitted orders. Weekend probe
   success proves transport only; calendar-gated observe validation needs a session.
5. **Duplicate/stale checks:** repeat an observe dispatch with the same slot while
   still within the deadline: one engine claim, other run `DUPLICATE_SLOT`. A later
   replay must be rejected as stale. Unit tests also prove a claim survives a crash,
   a timezone-equivalent timestamp deduplicates, and callback delay cannot allow a
   late pipeline start. Do not test these using `paper-execute`.

`/status` requires `Authorization: Bearer <MONITOR_TOKEN>` and keeps separate run,
queue, job, engine and gate evidence. Missing run records do not prove dispatch was
never accepted if an API response was lost; that uncertainty is recorded. `/health`
returns 503 for a clock silent for three minutes or recent missing/failed slots.
An independent uptime monitor must poll `/health` and notify you for unattended
alerts. **No notification channel or external uptime monitor is configured by this
patch.** A healthy endpoint is not an alert subscription. API status outages remain
visible as `last_error`; do not interpret missing observations as successful work.

## Stop / rollback

Set Worker `ENABLED=false` and redeploy; existing GitHub runs may still finish.
Keep authority external while investigating so native delayed events remain inert.
To revert to native scheduling, stop the Worker first, wait for queued/running scans
to settle, then set `SCHEDULER_AUTHORITY=native`. Do not delete persistent slot claims
to retry old work. `paper-execute` or a paid service requires separate user approval.

Sources: [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[dispatch permissions and returned ID](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[NYSE sessions](https://www.nyse.com/trade/hours-calendars),
[Workers Free limits](https://developers.cloudflare.com/workers/platform/pricing/),
[D1 Free limits](https://developers.cloudflare.com/d1/platform/pricing/),
[cron propagation](https://developers.cloudflare.com/workers/configuration/cron-triggers/).
