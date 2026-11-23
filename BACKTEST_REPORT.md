# Research results — November 22, 2026

**No real-market profitability conclusion is supported.** The historical Alpaca
replay and AI-hybrid historical comparison remain blocked by missing local market
credentials and point-in-time archives. No parameters were optimized against the
user's earlier losses.

## Existing baseline understood

The original repository implements two technical setups: a 20-bar breakout with
volume expansion and a daily trend filter, and an EMA20 pullback recovery above
EMA50. It uses daily ATR stops and capped sizing. The user's prior 100-calendar-day
result (~18 trades, 22.2% winners, −$326.70, −1.31%, ~2.97% drawdown) is context only.
Its source replay files were not present; those metrics were **not reproduced**.

Confirmed research defects repaired: the historical request's total 10,000-bar
cap could truncate longer data; the old replay used the current bar's close when
sizing at its open; it recomputed targets from later entry prices; and the supported
application lacked a default maximum holding period. The new runner uses unbounded
SDK data pagination within bounded request batches, open-time information, fixed
planned targets, and 15-session exits. Legacy pure interfaces and regression tests
are retained separately.

## Executed synthetic engineering replay

Command:

```bash
.venv312/bin/python -m trading_bot replay --fixture tests/fixtures/market.json --archive tests/fixtures/events.json --compare
```

Inputs are synthetic: two symbols, 64 bars each, one engineered target exit and one
engineered adverse gap/stop exit. The calendar coverage fixture is also synthetic.
No model API call, real news, brokerage fill or investment result is represented.

| Metric | Fixture result |
|---|---:|
| Symbols / bars | 2 / 128 |
| Entries / closed trades | 2 / 2 |
| Winners / win rate | 1 / 50% |
| Realized simulated P&L | $7.56 |
| End marked equity | $25,007.56 |
| Return | 0.03025% |
| Profit factor | 1.03038 |
| Mean expectancy per trade | $3.78 |
| Bar-snapshot maximum drawdown | 0.02882% |
| Holds | Same-session engineered exits; not a swing-horizon validation sample |
| Remaining open positions | 0 |
| Calendar slots in observed span | 100%; not a full session or real-feed coverage claim |
| SPY benchmark | Unavailable: fixture has no SPY intraday series |

At 10/20/30 bps slippage, the same scenarios are scored with unchanged signal/risk
settings; results are stored in `reports/comparison.json`. Increasing cost can also
cause the net reward/risk gate to reject trades, so a cost scenario with no trades
must not be interpreted as profitable robustness. The event baseline has no event
signals in this fixture. AI comparison is **UNAVAILABLE**, not a zero-loss AI result,
because no contemporaneous approvals were supplied.

These numbers merely demonstrate deterministic accounting, adverse gaps, costs and
fixed-target handling. The sample was deliberately constructed and has no statistical
interpretation. Separate tests verify session-based exits, same-bar stop-first
ambiguity, stale signals, missing calendars, future evidence and no next-bar fills.

## Actual research capabilities and limits

Outputs include signal/rejection funnels, trades, realized/unrealized P&L, win rate,
average winners/losers, profit factor, expectancy, drawdown, exposure, holding sessions,
setup/regime/symbol/sector performance, exit counts, equity curve, coverage and SPY
comparison when supplied. `--compare` holds the universe/period/capital/risk settings
constant across technical/event/AI modes and evaluates cost sensitivity.

Baseline modes do not fabricate an AI approval: they call the shared deterministic
risk function directly in a broker-free simulator. Execution always calls the AI
validation wrapper as well. Default research also requires calendar coverage. To
study the original technical-only baseline without historical event coverage,
explicitly run a research-only invocation such as:

```bash
REQUIRE_EVENT_CALENDAR=false python -m trading_bot replay --days 100 --strategy technical
```

That command was **not run successfully here** because Alpaca credentials are absent.
It weakens the historical event filter deliberately; compare all research modes with
the same setting and do not copy that override into guarded execution.

Historical AI can only consume archived candidate/decision/news packages. News
publication/update times must be no later than the historical signal; retrieval
must precede the recorded decision; the decision must precede entry and remain valid.
Today’s AI is never asked to invent a past approval. Missing/changed candidate hashes,
news, events or chronology prevent approval. Forward archives are emitted by runtime.

Remaining methodology limits: current-universe survivorship; split-adjusted provider
history may contain later corrections; missing delisted names; IEX exchange bias;
modeled spreads and fills rather than a historical order book; no full halt,
queue-position, partial-fill or liquidity-impact simulation; no dividends; no
statistical confidence claims from sparse samples. Stop-first is an explicit adverse
assumption, not reconstruction of intrabar sequence. A 30-second publication grace
means an entry waits for a later available bar open.

Long-period chronological development/validation/out-of-sample testing has not run.
Forward paper observation and timestamped archives are the defensible next step.
There is currently **no evidence that AI filtering improves this strategy**.

## Scaling measurement

Executed `.venv312/bin/python -m scripts.benchmark_scan` on this Mac/Python 3.12:

| Synthetic symbols | CPU scan seconds |
|---:|---:|
| 100 | 0.216 |
| 250 | 0.421 |
| 500 | 0.838 |
| 1,000 | 1.658 |

Every synthetic input was intentionally signal-producing. This measures Python
calculation only; no network, entitlement, liquidity discovery or provider rate-limit
performance is included. Real 500–1,000-symbol throughput remains **BLOCKED** pending
credentialed feed and cold-start measurements.
