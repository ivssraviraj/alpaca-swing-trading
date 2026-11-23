# Strategy and risk rules

The rules are experimental. They are not optimized against the user's previous
18-trade losing replay. Implementation correctness and positive fixture P&L do
not establish an edge. Long-only whole-share US stocks/ETFs; no live money,
margin borrowing, options, crypto, shorts, or forced trade quota.

## Technical setups

All signal calculations use ordered, completed regular-session five-minute bars
and prior-day daily bars. A 30-second publication grace excludes forming bars.
At least 61 intraday observations and 30 daily observations are required. Missing
recent five-minute confirmation slots or insufficient measured coverage defer signals.

| Setup | Objective confirmation |
|---|---|
| Breakout | Close above previous 20-bar high, volume ≥1.25× previous 20-bar mean, daily trend; retained original daily filter |
| Pullback | EMA20 above EMA50, previous close at/below EMA20, current close recovers EMA20, volume ≥0.85×; retained original daily filter |
| Breakout and retest | Break above an earlier resistance window in an earlier bar, later support retest, then close above preceding high with volume ≥1× |
| Trend continuation | Daily uptrend, EMA20 above EMA50, previous close above EMA20, new three-bar high, volume ≥1.1× |
| Relative-strength breakout | Breakout plus 20-session excess return over SPY >3 percentage points |
| Post-earnings momentum | Breakout with a timestamped release-style news event in the prior five sessions |
| Gap and go | Regular-session gap ≥2%, at least three completed opening bars, subsequent break of opening range, volume ≥1.25× |
| Post-earnings drift | Recent release, trend continuation, positive excess return over SPY |
| Earnings reversal | Recent release, session gap ≤−3%, three-bar lows above earlier stabilization low, EMA20 recovery, break of prior three-bar highs, volume ≥1.25× |

News headline classification discovers a possible release; it does not prove a
beat or bullish thesis. AI must evaluate actual supplied content and may reject it.
Earnings names are prioritized before ordinary candidates; discovery is capped
by `EVENT_DISCOVERY_LIMIT` and its actual coverage is audited. No candidates are
invented to fill the shortlist. At least five distinct candidates may be reviewed
when present; at most three paper entries per scan, often fewer.

Initial stop distance is max(1.5× daily ATR14, 1.5% price), capped at 7% price.
Target defaults to 2R. Entry zone is 0.2% below reference through a 0.5% no-chase
ceiling. Costs can make a nominal 2R setup fail the minimum 1.5 net reward/risk
screen. Whole shares are sized at the ceiling, not a more favorable old price.

Regime uses SPY, QQQ and IWM relative to 50-session averages. All three positive
is SUPPORTIVE; all negative is HOSTILE; otherwise NEUTRAL. Hostile halves the
risk budget instead of blocking every trade. Realized volatility, defensible
sample breadth and available sector ETF relative strength are supplied as evidence.
Sample breadth is not full-market breadth. Missing macro/fundamental information
is explicit. A configured high-impact macro event within 30 minutes blocks entries.

## AI approval

`AI_ENABLED=true` enables the official OpenAI API, separately configured from
ChatGPT subscriptions or scheduled tasks. There is no implicit internet access:
company-specific news excerpts, source URLs, publication/update/retrieval times,
candidate levels, market context and relevant events are supplied explicitly.
News is untrusted data, separated from system instructions; the model has no
execution tools. Schema compliance does not prove truth, so citations must exactly
match supplied provider evidence and all deterministic checks run afterward.

APPROVE for the exact symbol, affirmative factors, coherent explanation, concrete
invalidation conditions, nonempty verified evidence, no unresolved critical risks
or missing critical facts, and an unexpired decision are required. REJECT,
UNCERTAIN, refusal, malformed data, API failure, exhausted budget or unavailable
news never authorize a new entry. Confidence is uncalibrated and is not a win
probability or position-sizing input.

Approvals bind to complete candidate and evidence fingerprints plus model/prompt.
Material new news, changed technical conditions, expiration or price outside the
entry zone invalidates authorization. Every AI call reserves a conservative cost
estimate before submission. Timeout reservations are not refunded. Configure
pricing estimates for the selected model and a provider-side project spending cap;
local estimates cannot guarantee a bill if configured prices are wrong.

## Deterministic risk and exits

Defaults: $25,000 strategy capital; 0.75% planned risk per trade; 3% concurrent
planned risk; 12% position and 60% portfolio exposure; $3,000 maximum order;
four positions; two per sector (unknown names share an UNKNOWN bucket); no more
than two existing highly correlated peers at ≥0.85 correlation. Inadequate
correlation history blocks additional positions. Cash and buying power constrain
sizing independently; broker paper equity is never assumed to be $25,000.

Strategy realized/unrealized P&L is tracked separately from broker account equity.
Sizing equity is capped at initial strategy capital, so gains do not silently
increase the risk allocation. Pending buy quantities reserve exposure and risk.
Existing exposure is conservatively marked, with a slippage allowance.

Regular-session entries avoid the first 15 minutes and final 25 minutes, including
early closes; both exchange calendar and broker clock must permit entry. Upcoming
earnings within two sessions block entries by default; an unavailable earnings
calendar also blocks entries. A dedicated IEX execution acknowledgement is required.

Maximum holding period is **15 trading sessions after the entry session**; 16–20
requires an explicit extension acknowledgement. Owned GTC stops and targets are
reconciled against actual quantities, not requested entry quantity. Imminent next-session
earnings trigger a deterministic risk exit when calendar evidence is available.
Optional technical breakdown exit uses two completed daily closes below SMA20.
Textual AI invalidation conditions are audit information, not autonomous free-form
exit commands. Stop/target, time, earnings and optional technical exits implement
the objective thesis boundaries. Trailing-stop replacement is deliberately not
enabled: it needs a separate validated broker cancel/replace policy.

A limit bracket is submitted; if entry remains working after re-read, cancellation
is requested immediately rather than waiting for the next cron. Partial fills can
be temporarily unprotected because bracket children activate after full fill.
Recovery cancels conflicting owned orders, waits for terminal states, rereads the
position, then submits OCO protection or an owned-quantity close if a level is
already breached. No blind retry and no automatic takeover of manual holdings.
Stops cannot guarantee a loss cap through gaps, halts or broker outages.

## Research discipline

The current replay shares entry sizing and portfolio gates. It models bar-open
quotes with configurable spread, ≥10 bps slippage each side, fees, gap stops,
stop-first ambiguous OHLC bars, publication delay, conservative limit fills and
session time exits. Returns, expectancy, profit factor, drawdown, holdings,
exposure, setup/regime/symbol/sector metrics, rejection funnel, coverage and SPY
comparison (when SPY data exists) are emitted. `--compare` keeps the same inputs
and settings for three strategy modes and compares 10/20/30 bps slippage.

Use chronological development/validation/out-of-sample windows; do not select
rules from the full result. Start with at least 60%/20%/20% date partitions and
retain prior warmup bars without including warmup trades in the scored window.
No historical edge or out-of-sample claim has been made here because market data
and point-in-time AI archives were not supplied. Forward observation is the next
research step; see BACKTEST_REPORT.md.
