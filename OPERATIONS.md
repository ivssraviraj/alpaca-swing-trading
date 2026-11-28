# Operations and recovery

## Startup

1. Use a dedicated Alpaca paper account. Do not mix manual trades or the old `sw-`
   strategy with this executor. Old holdings need separate operator reconciliation;
   no automated ownership migration is inferred from a ticker or prefix.
2. Install the pinned environment, copy `.env.example`, and configure secrets privately.
3. Run configuration validation, offline tests, read-only Alpaca check and observation.
4. Configure OpenAI API billing/model/rates and run the small `check-ai` command.
   Enable AI for observation only after confirming spend limits and evidence retrieval.
5. Configure a Finnhub earnings-calendar entitlement for default event checks.
   Review actual universe counts, coverage, timestamps, news and rejected-candidate audit.
6. Before scheduled AI/execution, configure persistent PostgreSQL, verify locking,
   crash recovery and backup restoration against the actual service. Never use an
   ephemeral runner SQLite file as trading state.
7. Complete the external acceptance scenarios in TEST_REPORT.md. This delivery
   does not authorize automatic paper execution or claim those scenarios passed.

## Debugging a workflow run

Detailed diagnostics are on by default: `LOG_LEVEL=DEBUG` and
`DEBUG_TRACES_ENABLED=true`. GitHub Actions exposes both as repository/environment
variables. The engine writes structured JSON to stderr and `reports/runtime.jsonl`;
stdout remains the final command result. Each invocation has a UUID `run_id`, mode,
GitHub run/attempt and commit SHA when available. Stage boundaries include duration;
failures include type, HTTP status when available, and source file/line/function,
without exception locals or raw provider messages.

Open the Actions run's **Summary** first. The scan funnel, first rejection counts,
setup pass counts, candidate rejection stages and recent warnings appear there.
Download the audit artifact for the details:

| File | What it explains |
|---|---|
| `runtime.jsonl` | Configuration without credentials; lock/cache decisions; provider requests, retries and latency; evidence, AI budget/decisions, risk inputs, broker operations, recovery, replay and reporting |
| `scan_summary.json` | Counts for the latest completed scan, including AI review attempts and risk approvals |
| `scan_diagnostics.json` | Initial scan of every universe symbol: coverage, bar freshness, indicator values, setup checks, stop distance, costs/reward-risk and first rejection |
| `audit.jsonl` | Cumulative durable business audit; debug traces include run IDs and initial/before-AI/after-AI scan phases |
| `universe.json` | Selected symbols, ranking inputs and sector labels, including on cache hits |
| `replay_scan_examples.json` | Up to three bar-level examples per scanner rejection reason; aggregate counts are in each replay strategy's summary |
| `run_summary.md` | Local copy of the GitHub summary |

First-failure counts are mutually exclusive. Setup checks are separate and can
overlap: a symbol can fail a stop-distance limit **and** have no matching pattern.
Pattern checks are unavailable when history/coverage/freshness fails first. A pattern
pass is not a trade approval; legacy checks, evidence, AI and risk gates still apply.
AI review attempts include disabled/failed/cached reviews, not just billed API calls.
Use run IDs when comparing artifacts; local reports may contain files from older runs.

Missing sector classifications produce an explicit warning. `UNKNOWN` remains one
shared concentration bucket. Supply a verified symbol-to-sector JSON file through
`SECTOR_MAP_PATH` (for example `{"AAPL": "Information Technology"}`); a runner must
have that file checked out or provisioned. Updating the mapping invalidates the
same-day universe cache. This change does not invent classifications or bypass limits.

To reduce verbosity later, use `LOG_LEVEL=INFO` and
`DEBUG_TRACES_ENABLED=false`. This disables detailed runtime events and new durable
debug traces/per-symbol artifacts while keeping business audits and scan summaries.
Keep at least INFO for complete GitHub lifecycle summaries. Existing logs are not
deleted. Durable debug rows accumulate, so account for them in retention planning;
do not remove fills, intents or recovery checkpoints to reduce log volume.

Runtime diagnostics redact configured secrets, sensitive fields, URL paths/queries,
and database connection strings. Raw HTTP headers/bodies and SQL parameters are not
logged. Business evidence remains in the audit/archive for reproducibility; those
artifacts still contain account/trade information and should remain private.

## Testing GitHub scheduler delivery

`Scheduler Heartbeat` runs at UTC minutes `01,16,31,46` every hour, including
weekends and exchange holidays. It also runs immediately when its own workflow
file is pushed to `main`, and supports manual dispatch. No initial manual run is
required to activate a schedule on the default branch.

`Scheduler Offset Probe` is a second dummy workflow at UTC minutes `07,22,37,52`,
also all day including weekends. It has the same isolation and timestamp receipt
as the first probe. This compares a fresh schedule away from the hour boundary
with the original schedule. Neither probe invokes the engine. Cloudflare setup was
deferred on 2026-11-28 at the user's request; native scheduling remains authoritative.

`Scheduler Minimal Probe` is a temporary five-minute test at minutes `02,07,12,...,57`.
It only prints the event, run ID, revision and clock time. It makes no API calls,
uses no tokens or secrets and has no checkout, environment or concurrency setting.
Use the API auditor to retrieve its creation/job timestamps. Disable this higher
frequency probe after the bounded diagnostic session.

The probe uses only Python's standard library and read-only GitHub Actions metadata.
It has no checkout, package installation, paper environment, trading credentials,
broker calls or shared trading concurrency group. Its two-minute timeout bounds
each invocation. It is an independent diagnostic of the same GitHub scheduler,
not a replacement scheduler or a fix for delayed cron events.

Inspect the run summary for event type, run ID, commit, UTC/New York timestamps,
and creation-to-job-start latency. A successful `push` or `workflow_dispatch` run
does not prove that cron works: filter Actions runs by `event=schedule`. Compare
the expected quarter-hour slots with actual run creation and job-start timestamps.
The probe cannot observe a missing invocation from inside that missing run, and
the API does not identify the original nominal firing time of a delayed event.

List recent automatic deliveries:

```bash
gh run list --repo ivssraviraj/alpaca-swing-trading \
  --workflow scheduler-heartbeat.yml --event schedule --limit 30 \
  --json databaseId,createdAt,status,conclusion,url
```

Each probe requests 96 short runs/day (2,880 in a 30-day month). Together they request
192/day. The temporary five-minute probe adds 288/day while enabled. Disable the
diagnostic workflows when the experiment is finished; do not
disable `swing-scan.yml` as part of probe cleanup. These diagnostic changes do not
change the Swing Engine's schedule or execution settings.

Generate a read-only, bounded API audit using the repository's Python environment:

```bash
python scripts/audit_schedule.py \
  --since 2026-11-22T00:00:00-05:00 --include-engine-logs \
  --output output/scheduling/native-audit.json
```

The JSON separates expected market-session slots, actual creations, job starts,
manual runs, and parsed final engine results. Times include New York and UTC.
It applies the NYSE calendar, including early closes and DST, to the engine slots.
Expected schedules come from the recorded current `main` revision: split audit
windows if historical cron definitions or enabled state changed. A run created inside a slot's
15-minute window is only a **creation-window observation**. GitHub does not expose
the original nominal firing timestamp, so do not relabel it as proven on-time
delivery or assign off-hours runs to specific missing slots.

The 2026-11-28 audit found all 92 regular-session creation windows empty for
November 23–27 (26 + 26 + 26 + 0 + 14). Eight scheduled engine records existed,
all outside regular sessions; jobs started 3–4 seconds after creation. Four ended
at `ENTRY_FREEZE`; four completed `observe` scans with zero submissions. A separate
manual dry run on November 25 waited 178 seconds before its job started. That
manual queue delay does not explain absent scheduled run records.

On November 28, the original dummy was disabled and re-enabled at 14:56:26 and
14:56:32 EST respectively, with API states verified. The offset dummy was deployed
at 14:50 EST. Treat these as recovery experiments, not a proven scheduler repair.
Do not implement catch-up trades for old slots based on a delayed cron delivery.

## Entry freeze and exit management

`KILL_SWITCH=true` blocks new entries. Keep `paper-execute`, paper credentials and
execution gates enabled if the application must repair protection or enforce time
exits. AI can be unavailable or disabled while owned exits are managed.
`PAPER_ORDERS_ENABLED=false` disables **all application broker mutations**. Existing
broker-side GTC protection remains, but scheduled time exits and repairs cannot run.
Observation, replay and dry-run are always read-only regardless of environment flags.

Do not cancel GTC stop/target orders merely to pause the scanner. Emergency flatten
is a deliberate operator action in the **paper** dashboard: verify ownership,
cancel conflicting exits, wait for terminal states, reread actual quantity, then
close and verify the fill. There is no unguarded bulk-flatten CLI.

## Recovery states

| Condition | Application action | Operator action |
|---|---|---|
| Missing/expired AI or news | No new entry; continue reconciliation | Review evidence/configuration; no retry that bypasses approval |
| Entry timeout after submission | Query deterministic client ID; preserve ambiguous intent; never resubmit blindly | Inspect paper broker and durable intent; reconcile exact ID |
| Partial entry | Record only actual cumulative fill delta; cancel remaining entry and verify exit quantity | Validate real partial-fill/OCO behavior before unattended use |
| Missing stop or target coverage | Block new risk, emit urgent alert, cancel conflicting owned orders, wait, reread position, attempt OCO or close | Inspect broker immediately, especially if cancellation/recovery fails |
| Pending cancellation | No competing exit submitted; next invocation reconciles | Broker dashboard if pending state persists |
| Bracket fills during cancellation | Reread broker position/fills; do not submit a second sell | Confirm resulting flat state |
| Unowned or mixed/manual position | Never modify it; freeze new entries | Separate accounts or perform explicit audited ownership reconciliation |
| Database unavailable / writer lock held | Fail closed; no new broker request | Restore service or wait for current writer; never bypass lock |
| IEX data missing/stale/sparse | Candidate deferred with coverage evidence | Verify account entitlement; never silently use a delayed feed |
| Sheets/webhook failure | Audit failure; preserve local/durable fill; retry reporting only | Repair destination; never repeat an order to fix reporting |

A reserved intent with no broker acknowledgement is deliberately sticky. There is
no automatic reset command that could duplicate a late-accepted order. Investigate
broker history, all open orders, current positions and the exact client ID before
an operator changes its database state. Back up the journal first. Existing recovery
IDs are also never recycled; a failed/canceled recovery needing another order requires
operator review. This conservative behavior trades availability for safety.

## Notifications and ledger

Only confirmed BUY/SELL fill deltas, Saturday weekly summaries and urgent safety
failures can use the webhook. No no-trade, rejection, submission or routine success
alerts are sent. Times in trade messages use America/New_York; the ledger uses UTC.
Durable deduplication is applied after acknowledgement. Generic webhooks provide no
transactional exactly-once delivery: a crash after delivery but before checkpoint can
repeat an alert. The stable event key identifies it. Conversely, no configured webhook
means no external notification; logs/artifacts do not magically trigger ChatGPT tasks.

Google Sheets is disabled by default. Only `Alpaca Paper Fills` is inspected/created;
its exact header must match before append. Writes use RAW cell values and unique fill
keys. Existing `Investing`, `Swing Trading`, `Swing Scan Audit`, and `Alpaca Paper Orders`
are not rewritten. The historical NVT loss and $24,877 simulated balance are not
imported into this broker ledger. Schema mismatch or credential failure is reported
as FAILED, never successful persistence. The actual sheet was not accessed in this work.

## Operational limitations requiring validation

The optional [external scheduler setup](infra/scheduler/README.md) is disabled by
default. It dispatches a secret-free probe or explicit `observe` slots. Activating
external authority requires durable slot claims and suppresses native scheduled
scans; the existing concurrency lock alone does not deduplicate sequential runs.

GitHub cron can delay, drop or terminate runs. Broker GTC exits work independently,
but a partially filled bracket can leave a protection gap until recovery. A crash
between submitting an entry and requesting cancellation can leave an entry working;
its price ceiling remains, but validity expiration cannot be enforced during outage.
Cancel/recover also has a temporary unprotected window. Orders can race manual
activity outside the single-writer system. A dedicated paper account is required.
These are reasons to withhold unattended execution readiness, not hidden guarantees.

IEX may omit intervals with no prints and cannot certify consolidated liquidity or
spread. Feed access must be verified with the actual key; `check-alpaca` verifies the
configured feed only. Start small and measure real request duration before expanding.
Cache values and audits accumulate; establish database backup/retention before long
runs. Do not delete fill checkpoints, intents or cost basis during cache cleanup.
Keep artifacts private: they contain trade/account/evidence history, though no keys.

Weekly reports include forward price markouts for up to 1,000 recent rejected
candidates over five and fifteen sessions, when the exact observation day exists.
They are labeled as non-trades, exclude future/uncompleted daily data, and never
claim that a price increase was realizable foregone P&L. Missing days stay missing.

The optional economic calendar rejects timestamps without an explicit timezone.
Unavailable calendar/fundamental/consensus fields remain missing. News is provider
supplied and independently source-bound; the application does not fetch every source
URL or certify the publisher's truth. Do not treat model confidence as calibrated.

## Optional paper smoke test — deliberately not run

After review and all normal gates, run:

```bash
SMOKE_TEST_NOTIONAL_CAP=50 python -m trading_bot paper-order-smoke --ack SUBMIT_TINY_PAPER_ORDER
```

This is a real paper-order opt-in, capped at $50 by default, with the same technical,
evidence, AI, calendar and risk requirements. A high-priced symbol or absent valid
setup means **no order**. It does not manufacture a test trade. Inspect entry fill,
protective legs, cancellation, partial fills and final owned quantity at Alpaca.
Never point any command at a live endpoint; configuration rejects it.
