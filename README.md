# Alpaca Swing Trading — consolidated paper-only engine

**Purpose:** one repo-ready replacement for the earlier `engine.py` observation prototype. It runs on GitHub Actions using Alpaca **paper** trading only. It does not touch real money. It includes historical observation, a chronological replay, signal generation, capped order sizing, broker-managed bracket exits, restart-aware order reconciliation, an optional Google Sheets audit tab, and optional webhook notifications.

> **Strategy disclaimer:** This code is an **experimental breakout/pullback strategy**, NOT an exact port of the earlier ChatGPT daily swing-scan automation. Earnings-driven event selection, consensus/SEC fundamentals, consolidated SIP execution data and validated statistical edge are **not** automated. Never infer a trading advantage from a green workflow or a profitable short replay. GitHub Actions is best-effort scheduling, not a live trading server.

## One-time repository update (replace prior prototype in one commit)

Unzip, then copy the following **entire contents** into the root of your existing private GitHub repository:

```text
alpaca-swing-trading/
├── engine.py
├── strategy.py
├── replay.py
├── requirements.txt
├── README.md
├── .gitignore
├── tests/
│   └── test_engine.py
├── reports/
│   └── .gitkeep
└── .github/workflows/
    ├── swing-scan.yml    ← replaces previous observation workflow
    └── validate.yml      ← unit tests on commit
```

The old `test_connection.py` and `alpaca-test.yml` can remain: they do not place trades. **Do not retain another active scheduled swing workflow** under a different filename or duplicate runs can occur.

If using Terminal and you have the existing repository checked out:

```bash
# Extract ZIP into Downloads, then run from inside your existing repo:
cp -R ~/Downloads/alpaca_swing_complete/. .
git add engine.py strategy.py replay.py requirements.txt README.md .gitignore tests reports .github/workflows
 git commit -m "Consolidate Alpaca paper swing engine"
git push
```

The copy command is illustrative; adjust the extracted folder's location. On GitHub Web, you can upload the same files using **Add file → Upload files**, keeping the folder paths intact.

## Existing GitHub secrets (leave unchanged)

- `APCA_API_KEY_ID`: Alpaca **paper** API key
- `APCA_API_SECRET_KEY`: Alpaca **paper** secret

**Never** paste credentials in chat, commit JSON credentials to your repo, or hardcode keys.

## Initial safe verification

1. Push all files to the **default branch**. Wait for `Validate Swing Code` to show green (offline unit tests).
2. Open **Actions → Alpaca Swing Engine → Run workflow**.
3. Select **`replay`**. When completed, open the downloadable `alpaca-replay-<run_id>` workflow artifact. It contains `replay_summary.json` and `replay_trades.csv`.
4. Run the workflow again choosing **`observe`**. It checks the latest historical candle, even if markets are closed.
5. Keep paper execution disabled until you review the replay and Alpaca account contents. A green workflow is not proof that signals or fills are economically sound.

### Modes

| Mode | Data and behavior | Orders? |
|------|-------------------|---------|
| `observe` | Latest completed-looking IEX 5-minute bar; scans breakout and EMA pullback | Never |
| `replay` | 45 calendar days of 5-minute bars; next-bar-open, OHLC assumptions | Never |
| `paper` | Fresh completed 5-minute bar, Alpaca open clock, risk & idempotency checks, GTC paper bracket | **Only after explicit approval switches** |
| `report` | Alpaca paper account, positions, order states, optional Sheets + webhook | Never |

### Optional execution switches (ONLY after review)

In **Settings → Secrets and variables → Actions → Variables**, add:

| Variable | Value | Notes |
|----------|-------|-------|
| `PAPER_ORDERS_ENABLED` | `true` | Default is false |
| `PAPER_ONLY_ACK` | `YES_PAPER_ONLY` | Required second approval gate |
| `KILL_SWITCH` | `false` | Set to `true` to immediately prevent **new** orders at the next run |
| `STRATEGY_CAPITAL_USD` | `25000` | Caps sizing even if default Alpaca paper equity is $100k |
| `SYMBOLS` | `SPY,QQQ,IWM,AAPL,MSFT,NVDA` | Optional comma-separated stock/ETF universe |

With both approval switches enabled, **scheduled weekday runs use `paper` mode**; otherwise the schedule uses `observe`. To test on demand, choose `paper` manually. **Paper is the only supported environment**; the TradingClient is hardcoded `paper=True`.

To stop new orders, set `KILL_SWITCH=true` or `PAPER_ORDERS_ENABLED=false`. **Important:** this stops the bot creating new orders, but **does not cancel existing broker brackets or liquidate positions**. Inspect the Alpaca paper dashboard for any open positions; broker-side GTC stops/targets remain in place unless separately canceled, rejected, or filled.

### Trading limits (defaults in `strategy.py`)

- Strategy sizing capital: **min(Alpaca paper equity, $25,000)**, not the default $100,000 Alpaca paper equity.
- Risk per entry ≤ **0.75%** of strategy capital (plus market/slippage uncertainty).
- Aggregate estimated open stop risk ≤ **3%** of strategy capital.
- Per-position exposure ≤ **12%**, portfolio exposure ≤ **60%**.
- Max simultaneous positions: **4**; **1 new bracket per run**, whole shares, long only, no leverage.
- Stop distance **1.5–7%**; default take profit **2R** using daily ATR-based stop distance.
- Broker-managed **GTC bracket exits**: take-profit limit + stop-market. Stops are not guaranteed to fill at the stop price and regular-session gaps can cause bigger losses.
- New entries require the Alpaca clock to be open, >25 minutes to market close and fresh completed bars (≤20 minutes old). No delayed-market backfills.
- Open positions and broker order history are fetched before each new entry; unprotected, unrelated, or unrecognized positions/orders cause **fail-closed** behavior.
- Every entry uses a deterministic `client_order_id` based on symbol and signal bar timestamp. Ambiguous submission failures are **not** blindly retried.

**Optional, off by default (experimental):**
- `ENABLE_TIME_EXITS=true` and `TIME_EXIT_CALENDAR_DAYS=21`: cancel bracket legs, wait for cancellation, then request broker position close. This creates a brief **unprotected gap** and requires active monitoring. Do not enable without testing the sequence in Alpaca paper first.
- `ENABLE_TRAILING_STOPS=true`: after a 3% unrealized move, request an upward-only Alpaca stop-leg replacement. Replacements can fail and must be checked in broker order history.
- `EARNINGS_DATES_JSON={"AAPL":"2026-12-01"}`: manually-maintained blackout dates, no entries within ±3 days. This is **not an earnings-calendar service**.

### Google Sheets (optional, does not block trading)

The engine only appends to a **new tab** named `Alpaca Paper Orders`; it never overwrites your existing `Swing Trading`, `Investing`, or audit tabs.

1. Create a Google Cloud service account, enable Google Sheets API (and Google Drive API if your project requires it for sheet access), generate its JSON key securely.
2. **Share your existing Google Sheet with the service account email as Editor**.
3. In GitHub Actions **Variables**, set `GOOGLE_SHEET_ID` to the spreadsheet ID from its URL (not the full URL).
4. Store base64-encoded JSON as GitHub Actions **Secret** `GOOGLE_SA_JSON_BASE64`. On macOS/Linux: `base64 < service-account.json | tr -d '\n'` and paste output into GitHub's secret field. Do not copy the base64 string into your code or this chat.
5. Run `report`. It creates the new tab and appends **deduplicated status changes** for paper order parents/legs. If Google is unavailable, Alpaca broker history remains the authority.

If you do not configure these two settings, trading and scanning still work, but your existing Sheet does **not** automatically sync.

### Notifications (optional)

- Secret: `ALERT_WEBHOOK_URL` (Discord or Slack incoming webhook URL)
- Variable: `ALERT_WEBHOOK_KIND` = `discord` or `slack` (default `discord`)

Paper order submission and certain critical alerts are sent immediately if configured. Weekly Saturday reports use the same webhook. No webhook means messages appear only in workflow logs.

## Free plan / scheduling limits

- GitHub workflow has a **15-minute** weekday schedule in a UTC envelope covering Eastern daylight/standard time. It also runs a Saturday report. The **Alpaca clock** determines whether the exchange is open, including holidays and early closes.
- GitHub can **delay or drop** scheduled runs. Market data on the free **IEX** feed is a single exchange, not consolidated SIP. Signals/volumes can differ significantly from full-market data.
- A 15-minute schedule can miss short-lived signals; no claim of high-frequency or continuously monitored execution. Bracket exits are handled by Alpaca's broker order service, subject to its order rules.
- Your personal free GitHub Actions quota depends on account type/usage. Check **Settings → Billing → Actions**; not guaranteed unlimited free runs.
- The 45-day replay uses Alpaca IEX candles, simplified next-bar fills, conservative stop-first OHLC ordering and a 0.1% simulated slippage per side. It ignores transaction costs, real spreads, volume impact and data survivorship. Results are **illustrative**, not evidence of alpha.

## Future work / explicitly NOT included

- A verified replica of your existing ChatGPT job's earnings-driven momentum, SEC filings and analyst consensus signals.
- Consolidated real-time SIP data, reliable execution SLAs or websocket trade-update service.
- Reconciliation of your old ChatGPT paper ledger (previous balance and NVT trade) against Alpaca's independent paper account; the accounts **are not merged**.
- Deployment to a production-grade always-on broker service or any real-money trading.

## Troubleshooting

- `PAPER ORDER LOCKED`: correct by using `observe` or deliberately enable BOTH approval gates after reviewing the code.
- `Unmanaged position`: use a dedicated Alpaca paper account; do not let this engine manage manual holdings.
- `PROTECTION ALERT`: Alpaca has a position with no recognized active protective stop. **Inspect the account directly**; new entries are blocked.
- `stale/incomplete bar`: market just opened, delayed Github schedule, or IEX data was insufficient; correctly skips that entry.
- `No IEX data`: permission/feed issue or market data unavailable; engine exits without orders.
- `Ambiguous order response`: inspect Alpaca for the deterministic client order ID before trying anything else; never manually duplicate a market order.
- A green `observe` run after market close is normal and does **not** place trades.

**Run `Validate Swing Code`, then `replay`, then `observe`. Orders should remain OFF until you deliberately approve paper execution.**
