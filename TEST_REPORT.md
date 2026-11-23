# Local validation — November 22, 2026

Final offline result: **119 passed**, no warnings, in **4.16 seconds** on this Mac
with Python **3.12.16**. Application statement coverage: **84%** (1,502/1,788
statements); execution adapter 86%, AI service 87%, models 98%. Coverage is not a
proof of broker correctness. The unexecuted real-service CLI branches are explicitly
excluded from any readiness claim.

## Executed checks

Commands below were run from the repository root using `.venv312`.

| Group | Status | Actual command / result |
|---|---|---|
| Full offline suite and coverage | PASS | `.venv312/bin/python -m pytest --cov=trading_bot --cov-report=term-missing --cov-report=json:reports/coverage.json -q` — 119 passed |
| Original regression suite | PASS | Included above: all 10 original tests pass |
| Lint | PASS | `.venv312/bin/python -m ruff check .` |
| Formatting | PASS | `.venv312/bin/python -m ruff format --check .` |
| Scoped type checks | PASS | `.venv312/bin/python -m mypy` — config, shared models and risk module; not a claim of whole-repository typing |
| Bytecode compilation | PASS | `.venv312/bin/python -m compileall -q trading_bot engine.py strategy.py replay.py data_audit.py` |
| Editable package install | PASS | `.venv312/bin/python -m pip install --no-deps --no-build-isolation -e .` |
| Dependency consistency | PASS | `.venv312/bin/python -m pip check` — no broken requirements |
| Configuration defaults | PASS | `.venv312/bin/python -m trading_bot validate-config` — orders and AI disabled, IEX, target universe 100 |
| Fixture replay/comparison | PASS | `.venv312/bin/python -m trading_bot replay --fixture tests/fixtures/market.json --archive tests/fixtures/events.json --compare` |
| CPU scaling | PASS | `.venv312/bin/python -m scripts.benchmark_scan` — 100/250/500/1,000 synthetic symbols, no API throughput claim |
| Workflow YAML and safety structure | PASS | `.venv312/bin/python scripts/check_workflows.py` — three YAML files, one scheduler, no order mode on push |
| Secret-pattern and ignore checks | PASS | `.venv312/bin/python scripts/secret_scan.py` — no detected key/private-key patterns; no values printed |
| Diff whitespace | PASS | `git diff --check` |

The first local environment used Python 3.9; an isolated Python 3.12 environment was
installed without replacing it. Tests initially exposed fixture/API-contract issues;
those were corrected and rerun. NumPy was constrained to the tested 2.3 series to
remove the pandas/calendar deprecation flood. The table describes the final result,
not an assertion that every intermediate run passed.

## Test groups

| File | Count | Exercised behavior |
|---|---:|---|
| `tests/test_engine.py` | 10 | Original pure strategy/sizing/replay/safety regression interfaces |
| `tests/test_core.py` | 40 | Settings, timestamps, calendar, warmup, regime, liquidity, risk limits, cache/budgets, context binding, durable and cross-process locks |
| `tests/test_pipeline.py` | 31 | Full fixture buy/exit/ledger/notification flows, winning/losing scenarios, failures, read-only modes, duplicate/restart/partial-fill behavior |
| `tests/test_providers.py` | 8 | Actual SDK parsing/pagination contracts with synthetic responses, transient/rate-limit retries, data failure and daily universe reuse |
| `tests/test_recovery_reporting.py` | 8 | Protection recovery under entry freeze, time exits, cancellation races, manual additions, Sheets and notifications |
| `tests/test_research.py` | 9 | Shared risk simulation, delayed fills, gaps, costs, missing historical evidence, time exits and forward rejected-candidate markouts |
| `tests/test_setups_and_boundaries.py` | 10 | Remaining setup fixtures, bounded order pagination, mode/state guards, PostgreSQL mocked lock contract, incremental refresh and broker-free replay |
| `tests/test_calendar_reporting_integrations.py` | 3 | Finnhub-shaped calendar responses, macro timestamp validation and Saturday reporting/closed-trade metrics |

All provider responses and fills in these tests are **synthetic or mocked**, not
recorded real-account executions. The winning and losing end-to-end cases run
market bars → technical selection → news → mock OpenAI structured response → risk
→ mock Alpaca bracket/fill → exit → atomic fill ledger → mock webhook. Separate
negative scenarios cover AI rejection and ambiguous execution.

## Required failure-path traceability

1. AI timeout, rejection, uncertainty, malformed response → `test_fail_closed_paths_never_submit`.
2. Missing news, wrong symbol, missing evidence, unresolved risks, expired approval → same parameterized suite.
3. Stale/missing bars, changed signal/news, stale/chased quote → same suite.
4. Existing pending order, sector/correlation/aggregate risk/exposure/calendar constraints → `test_portfolio_risk_gates`.
5. Repeated identical run/restart → `test_duplicate_same_run_and_restart`.
6. Closed broker clock and kill switch → fail-closed suite; read-only modes → `test_read_only_modes_cannot_submit`.
7. Accepted or unacknowledged submission timeout → `test_submission_timeout_reconciles_never_retries`.
8. Existing exits under kill switch/AI-off → `test_missing_protection_recovery_with_kill_switch_and_ai_off`.
9. Wrong endpoint → `test_wrong_endpoint_rejected`; missing durable approved intent → `test_direct_submission_without_approval_intent_is_blocked`.
10. Partial fill quantities → `test_partial_fill_recovers_actual_quantity`.
11. Missing protection → recovery tests; cancellation pending/fill-during-cancel tests prevent double sells.
12. Failed Sheets write/schema mismatch → `test_sheet_schema_failure_no_writes_and_sync_dedup`.
13. Future historical news/event evidence → chronology and historical-event tests; no fabricated AI → research tests.
14. Two overlapping invocations → `test_single_writer_and_persistent_intents` and an actual separate Python process in `test_overlapping_process_is_blocked_by_same_durable_lock`.
15. Exhausted provider budget → parameterized pipeline and `test_ai_cache_budget_and_evidence_binding`.
16. Old/manual or mixed owned quantities → `test_unowned_manual_position_not_changed`, `test_manual_addition_to_owned_symbol_not_modified`.
17. No false execution alerts / failed sends not marked delivered → notification tests and the full end-to-end scenarios.
18. Calendar DST/holidays/early close, no forming candles, fixed time exits → core/research tests.

## Real services and other checks

| Integration | Status | Evidence |
|---|---|---|
| Alpaca account/data | BLOCKED | `.venv312/bin/python -m trading_bot check-alpaca` exited 2: missing paper credentials; no remote request made |
| OpenAI small call | BLOCKED | `.venv312/bin/python -m trading_bot check-ai` exited 2: missing `OPENAI_API_KEY`; no billable call made |
| Real paper dry-run | BLOCKED | `.venv312/bin/python -m trading_bot paper-dry-run` exited 2: missing paper credentials |
| Finnhub real events/macro | BLOCKED | No local `FINNHUB_API_KEY`; only response-contract tests ran |
| PostgreSQL real lock/restart/transaction behavior | BLOCKED | No database URL or local server; psycopg contract mocked, SQLite actually exercised |
| Google Sheets / webhook | BLOCKED | No local service-account credentials or destination; only mocks ran; existing sheet untouched |
| Real historical 100-day replay / long-period research | BLOCKED | No market credentials or historical artifacts supplied |
| Real historical AI comparison | BLOCKED / UNAVAILABLE | No contemporaneous approval/news archive; never substituted synthetic approvals |
| Real paper-order smoke test | NOT RUN | Requires separate explicit opt-in; no brokerage order was submitted |
| Live-money orders | NOT RUN; unsupported | Fixed paper endpoint and validation; no live configuration exists |
| GitHub Actions runner | NOT RUN | User owns the final push; local structural checks only |
| `actionlint` | NOT RUN | Binary not installed; YAML parsing and targeted workflow safety assertions passed |

## Before guarded execution

Verify actual feed/news/quote/calendar entitlement and latency, durable PostgreSQL
serialization and restore, broker bracket/OCO behavior for partial fills, cancellation
and restart races, actual fill quantity accounting, and destination delivery. Review
IEX limitations and the temporary protection/entry-expiration gaps possible during
runner outages. Keep all execution acknowledgements unset until those results are
reviewed. **No unattended-execution readiness is claimed.**
