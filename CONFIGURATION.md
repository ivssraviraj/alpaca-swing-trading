# Configuration

Settings use environment variables first, then a private `.env`, then the defaults below.
No secrets are embedded in this reference. Unknown `.env` keys are ignored for compatibility.
Malformed typed values and unsafe endpoints fail closed. Run `python -m trading_bot validate-config`.

| Variable | Default | Meaning |
|---|---|---|
| `APCA_API_KEY_ID` | `""` | Alpaca paper key; secret. |
| `APCA_API_SECRET_KEY` | `""` | Alpaca paper secret; secret. |
| `APCA_API_BASE_URL` | `"https://paper-api.alpaca.markets"` | Only this exact paper origin is accepted; no live endpoint. |
| `OPENAI_API_KEY` | `""` | Separate programmatic OpenAI API credential; secret. |
| `OPENAI_MODEL` | `"gpt-5.4"` | Preferred GPT-5.4 review model with Responses structured outputs; verify availability and pricing. |
| `PAPER_ORDERS_ENABLED` | `false` | Opt-in broker mutations, only in paper-execute mode. |
| `PAPER_ONLY_ACK` | `""` | Execution requires YES_PAPER_ONLY. |
| `KILL_SWITCH` | `false` | Freeze entries; keep execution enabled to manage owned exits. |
| `EXECUTION_VALIDATED_ACK` | `""` | Execution requires PAPER_CHECKS_REVIEWED after external validation. |
| `STRATEGY_CAPITAL_USD` | `25000` | Initial virtual allocation and sizing cap, separate from broker equity. |
| `RISK_PER_TRADE` | `0.0075` | Planned trade risk fraction, capped at 1.5%. |
| `MAX_TOTAL_RISK` | `0.03` | Concurrent planned risk fraction, maximum configurable 4%. |
| `MAX_POSITION_FRACTION` | `0.12` | Per-symbol exposure fraction. |
| `MAX_TOTAL_EXPOSURE` | `0.6` | Portfolio exposure fraction. |
| `MAX_ORDER_NOTIONAL` | `3000` | Hard per-entry USD notional cap. |
| `MAX_POSITIONS` | `4` | Concurrent positions plus pending entries. |
| `MAX_PER_SECTOR` | `2` | Unknown sectors share UNKNOWN concentration bucket. |
| `MAX_CORRELATED_POSITIONS` | `2` | Maximum already-held peers above correlation threshold. |
| `CORRELATION_LIMIT` | `0.85` | Daily-return correlation threshold over up to 60 observations; at least 30 required. |
| `MIN_STOP_FRACTION` | `0.015` | Minimum reference-price stop distance. |
| `MAX_STOP_FRACTION` | `0.07` | Maximum reference-price stop distance. |
| `REWARD_TO_RISK` | `2` | Gross initial target multiple; net cost screen is separate. |
| `MIN_NET_REWARD_RISK` | `1.5` | Minimum expected reward/risk after modeled costs. |
| `MAX_HOLD_SESSIONS` | `15` | Sessions after entry session; 16–20 needs extension acknowledgement. |
| `EXTENDED_HOLD_ACK` | `false` | Explicit exception to default 15-session maximum, never above 20. |
| `EARNINGS_EXIT_ENABLED` | `true` | Exit owned positions ahead of known imminent next-session earnings. |
| `TECHNICAL_EXIT_ENABLED` | `false` | Optional two daily closes below SMA20 exit. |
| `MIN_PRICE` | `5` | Minimum price; cannot be below $5. |
| `MIN_DAILY_DOLLAR_VOLUME` | `1000000` | Median of prior 20 daily price×volume observations on selected feed; not consolidated if IEX. |
| `MIN_DAILY_BARS` | `30` | Daily indicator/liquidity warmup. |
| `MIN_INTRADAY_COVERAGE` | `0.8` | Required observed regular-session slot fraction. |
| `MAX_SPREAD_BPS` | `30` | Maximum verified quote spread, basis points. |
| `SLIPPAGE_BPS` | `10` | Modeled slippage per side, minimum 10 bps. |
| `SPREAD_BPS` | `5` | Modeled full spread; replay charges half each side. |
| `FEE_PER_SHARE` | `0` | Modeled per-share fee each side. |
| `NO_CHASE_FRACTION` | `0.005` | Maximum entry extension over signal reference. |
| `SIGNAL_TTL_MINUTES` | `15` | Signal life after candle close. |
| `MAX_QUOTE_AGE_SECONDS` | `60` | Age limit for quote at final risk check. |
| `ENTRY_CLOSE_BUFFER_MINUTES` | `25` | No entry this many minutes before session close. |
| `UNIVERSE_SIZE` | `100` | Requested selected count, 1–1000; actual coverage is reported. |
| `UNIVERSE_MODE` | `"dynamic"` | dynamic discovery or explicit static symbols. |
| `SYMBOLS` | `"SPY,QQQ,IWM,AAPL,MSFT,NVDA"` | Comma-separated static/replay names; dynamic universe ignores this restriction. |
| `SECTOR_MAP_PATH` | `""` | Optional local JSON mapping ticker to verified sector; unclassified names use UNKNOWN. |
| `BATCH_SIZE` | `50` | Symbols per market-data request batch. |
| `REQUEST_INTERVAL_SECONDS` | `0.35` | Minimum interval per provider client; calls are sequential. |
| `REQUEST_TIMEOUT_SECONDS` | `15` | Bounded provider request timeout. |
| `DATA_FEED` | `"iex"` | iex or sip; no silent fallback. |
| `IEX_EXECUTION_ACK` | `""` | IEX execution requires I_ACCEPT_IEX_LIMITATIONS. |
| `SHORTLIST_SIZE` | `5` | Maximum distinct symbols receiving candidate AI review; minimum configurable 5. |
| `MAX_ENTRIES_PER_SCAN` | `3` | Maximum executed entries, never above 3. |
| `NEWS_MAX_AGE_HOURS` | `120` | Maximum news age from publication; updates after decision time rejected. |
| `FINNHUB_API_KEY` | `""` | Optional licensed calendar credential; secret. Required for default event-risk checks. |
| `REQUIRE_EVENT_CALENDAR` | `true` | Missing upcoming earnings coverage blocks entries by default. |
| `EARNINGS_BLACKOUT_SESSIONS` | `2` | No new entry within this many sessions of scheduled earnings. |
| `EVENT_DISCOVERY_LIMIT` | `20` | Recent-earnings symbols with release-news retrieval per scan; audited coverage cap. |
| `MACRO_CALENDAR_ENABLED` | `false` | Optional Finnhub economic-calendar entitlement; strict timestamp checks. |
| `AI_ENABLED` | `false` | Allow programmatic billed AI review; false cannot authorize entries. |
| `AI_MAX_CALLS_DAILY` | `30` | UTC daily call cap, durable and pessimistic after timeout. |
| `AI_DAILY_BUDGET_USD` | `1` | Estimated UTC daily spend cap; also set provider-side limits. |
| `AI_INPUT_USD_PER_MILLION` | `2.5` | Uncached input rate for GPT-5.4; update when changing model or pricing. |
| `AI_OUTPUT_USD_PER_MILLION` | `15` | Output rate for GPT-5.4, including reasoning tokens; update when changing model or pricing. |
| `AI_MAX_INPUT_BYTES` | `24000` | Reject oversized evidence packages; never silently trim contradictory articles. |
| `AI_MAX_OUTPUT_TOKENS` | `1500` | Response token upper bound. |
| `AI_MIN_INTERVAL_SECONDS` | `2` | Durable call throttle. |
| `STATE_PATH` | `"state/trading.sqlite3"` | Local persistent SQLite path; all local invocations must use the same path. |
| `DATABASE_URL` | `""` | Persistent PostgreSQL connection string; secret; required for scheduled AI/execution. |
| `GITHUB_ACTIONS` | `false` | Automatically true on GitHub runners; do not override there. |
| `SHEETS_ENABLED` | `false` | Explicitly enable writes to dedicated Alpaca Paper Fills tab only. |
| `GOOGLE_SHEET_ID` | `""` | Target spreadsheet ID, not URL; never writes existing simulated portfolio tabs. |
| `GOOGLE_SA_JSON_BASE64` | `""` | Base64 service-account JSON, secret; share target sheet with that service account. |
| `ALERT_WEBHOOK_URL` | `""` | Optional HTTPS incoming webhook destination; secret. |
| `ALERT_WEBHOOK_KIND` | `"discord"` | discord uses content; slack uses text. |
| `REPORTS_DIR` | `"reports"` | Local audit/replay/report export directory; artifacts may contain private trading history. |
| `SMOKE_TEST_NOTIONAL_CAP` | `50` | Paper smoke-test cap, maximum $100; all normal approval/risk gates still apply. |

## Usage notes

Local configuration can use every variable above. The workflow wires common settings and all secrets explicitly; add another named variable to its `env` mapping when changing a less common default on GitHub. GitHub repository variables are not automatically OS environment variables.

GPT-5.4 is the preferred model. Its standard short-context rates were verified on 2026-11-22: $2.50 per million input tokens and $15 per million output tokens (https://developers.openai.com/api/docs/models/gpt-5.4). Cost reservations remain conservative: they estimate input tokens from bytes, include schema overhead and the output ceiling, and are not refunded. The $1 daily budget can therefore stop before 30 calls. Sharing allowances may reduce the provider bill but do not disable this local guard. The request does not set reasoning effort; GPT-5.4 currently defaults to none. Revalidate output limits, latency and rates before changing model or reasoning settings.

Paper execution gates are cumulative: mode `paper-execute`, `PAPER_ORDERS_ENABLED=true`, `PAPER_ONLY_ACK=YES_PAPER_ONLY`, `EXECUTION_VALIDATED_ACK=PAPER_CHECKS_REVIEWED`, valid paper credentials, durable state, and all normal technical/AI/risk gates. IEX additionally requires `IEX_EXECUTION_ACK=I_ACCEPT_IEX_LIMITATIONS`. None are set by this delivery.

Do not disable `REQUIRE_EVENT_CALENDAR` just to generate trades. Disabling it is an explicit research/operational choice with missing event-risk protection. Keep it true for guarded execution.

The legacy `ENABLE_TIME_EXITS`, `TIME_EXIT_CALENDAR_DAYS`, `ENABLE_TRAILING_STOPS` and `EARNINGS_DATES_JSON` switches no longer control the supported application. Time exits now default to 15 sessions. The preserved low-level legacy replay still accepts its explicit calendar-day argument for comparison.
