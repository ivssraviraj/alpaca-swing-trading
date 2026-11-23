# Alpaca AI-reviewed swing trading

**READY FOR OBSERVATION after read-only credential checks. Paper execution remains disabled.**
The application is implemented and locally tested; real Alpaca/OpenAI, PostgreSQL,
Sheets, and webhook validation is still required. See [TEST_REPORT.md](TEST_REPORT.md)
and [ACCEPTANCE.md](ACCEPTANCE.md) for the exact boundary. No profitability is established.

This Python application owns the workflow: broker reconciliation → daily liquid
universe → completed-candle technical signals → retrieved evidence → programmatic
OpenAI review → deterministic risk checks → guarded Alpaca **paper** brackets →
confirmed-fill ledger. The existing ChatGPT tasks, Investing portfolio, simulated
swing history, and legacy NVT trade are separate and were not modified.

## Local setup

Use Python **3.12**. The pre-existing `.venv` is Python 3.9 and was preserved.
The implementation was tested in `.venv312`.

```bash
python3.12 -m venv .venv312
source .venv312/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
cp .env.example .env
# Edit .env privately; never paste secret values into chat or commit them.
python -m trading_bot validate-config
python -m pytest
```

`requirements.lock` pins the runtime and developer packages used in validation.
`pip install -e ".[dev]"` is also supported but may select newer dependency versions.
On a fresh runner the application retrieves required history; caches are optional
optimizations, not prerequisites for correctness.

## Commands

| Command | Behavior |
|---|---|
| `python -m trading_bot test` | Offline pytest suite; no credentials required |
| `python -m trading_bot validate-config` | Validate settings without printing secrets |
| `python -m trading_bot check-alpaca` | Read-only paper account, clock, configured-feed bars |
| `python -m trading_bot check-ai` | One small billed structured-output API check; no trade authority |
| `python -m trading_bot observe` | Reconcile and scan; never submit/cancel orders |
| `python -m trading_bot paper-dry-run` | Full evaluation and risk proposals; never mutate broker orders |
| `python -m trading_bot paper-execute` | Explicitly gated paper entries and exit management |
| `python -m trading_bot weekly-report` | Write local report; webhook only on Saturday in New York |
| `python -m trading_bot replay --days 100` | Historical data retrieval and chronological research; no orders |

Run the complete offline fixture research without secrets:

```bash
python -m trading_bot replay --fixture tests/fixtures/market.json --archive tests/fixtures/events.json
python -m trading_bot replay --fixture tests/fixtures/market.json --strategy ai
python -m trading_bot replay --fixture tests/fixtures/market.json --archive tests/fixtures/events.json --compare
python -m scripts.benchmark_scan
```

The fixture is explicitly synthetic, including its calendar. Its engineered winner
and loser test mechanics, not an investment hypothesis. The AI command reports
`UNAVAILABLE` when no archived contemporaneous decisions exist. It never substitutes
bullish rules for an AI decision.

## Safe defaults and activation

`PAPER_ORDERS_ENABLED=false`, `AI_ENABLED=false`, `SHEETS_ENABLED=false`; no
notification destination is assumed. `paper-execute` needs every documented gate,
an available earnings calendar by default, evidence-bound affirmative AI approval,
fresh data/quotes, isolated ownership, sufficient risk capacity, and persistent state.
`PAPER_ONLY_ACK` never authorizes live trading. The only supported broker endpoint
is `https://paper-api.alpaca.markets`; no live fallback exists.

Use `KILL_SWITCH=true` to freeze entries **while keeping execution mode and its
credentials enabled for exit management**. Turning execution off makes every mode
read-only; existing broker GTC exits remain hosted at Alpaca, but the application
cannot repair or time-exit positions in read-only mode. See [OPERATIONS.md](OPERATIONS.md).

## Scope and limits

The dynamic universe defaults to 100 names and can request 1,000. IEX volumes and
quotes reflect a limited exchange, not consolidated US-market liquidity. The bot
reports actual returned/eligible/scanned counts and never silently switches feeds.
Daily universe discovery can require hundreds of API pages. Synthetic CPU timings
are not API throughput benchmarks. Start with `UNIVERSE_MODE=static` and the six
existing symbols for connectivity verification, then expand deliberately.

Alpaca news supplies timestamped excerpts. Optional Finnhub calendars require your
entitlement; no consensus, exact release chronology, SEC filing access, or analyst
revision is invented. Historical AI evaluation needs forward-captured records.
GitHub Actions is best-effort, not continuous monitoring; partial-fill recovery,
cancellation races, and broker outages remain important external acceptance tests.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): module boundaries, state, audit schema
- [FUTURE_ENHANCEMENTS.md](FUTURE_ENHANCEMENTS.md): planned Sheets watchlist, ChatGPT research handoff, and smaller-model validation
- [STRATEGY.md](STRATEGY.md): nine setups, AI criteria, costs and risk limits
- [CONFIGURATION.md](CONFIGURATION.md): every environment variable and default
- [OPERATIONS.md](OPERATIONS.md): ownership, failures, recovery, notification policy
- [BACKTEST_REPORT.md](BACKTEST_REPORT.md): executed research and limitations
- [TEST_REPORT.md](TEST_REPORT.md): commands and actual validation results
- [DEPLOYMENT.md](DEPLOYMENT.md): one commit/push and post-push checks
- [ACCEPTANCE.md](ACCEPTANCE.md): implemented, externally blocked, and deferred items

The original `strategy.py`, `replay.py`, and `data_audit.py` remain available for
legacy research and regression tests. `engine.py --mode ...` is a compatibility
wrapper for the new CLI; its old technical-only order path is removed. The old
low-level replay's optional calendar-day behavior is preserved for comparison;
the supported application replay and execution default to **15 trading sessions**.
