"""Generate safe defaults from field definitions, never from runtime secret values."""

import json
from pathlib import Path

from pydantic import SecretStr

from trading_bot.config import Settings

notes = {
    "apca_api_key_id": "Alpaca paper key; secret.",
    "apca_api_secret_key": "Alpaca paper secret; secret.",
    "apca_api_base_url": "Only this exact paper origin is accepted; no live endpoint.",
    "openai_api_key": "Separate programmatic OpenAI API credential; secret.",
    "openai_model": "Preferred GPT-5.4 review model with Responses structured outputs; verify availability and pricing.",
    "paper_orders_enabled": "Opt-in broker mutations, only in paper-execute mode.",
    "paper_only_ack": "Execution requires YES_PAPER_ONLY.",
    "execution_validated_ack": "Execution requires PAPER_CHECKS_REVIEWED after external validation.",
    "kill_switch": "Freeze entries; keep execution enabled to manage owned exits.",
    "strategy_capital_usd": "Initial virtual allocation and sizing cap, separate from broker equity.",
    "risk_per_trade": "Planned trade risk fraction, capped at 1.5%.",
    "max_total_risk": "Concurrent planned risk fraction, maximum configurable 4%.",
    "max_position_fraction": "Per-symbol exposure fraction.",
    "max_total_exposure": "Portfolio exposure fraction.",
    "max_order_notional": "Hard per-entry USD notional cap.",
    "max_positions": "Concurrent positions plus pending entries.",
    "max_per_sector": "Unknown sectors share UNKNOWN concentration bucket.",
    "max_correlated_positions": "Maximum already-held peers above correlation threshold.",
    "correlation_limit": "Daily-return correlation threshold over up to 60 observations; at least 30 required.",
    "min_stop_fraction": "Minimum reference-price stop distance.",
    "max_stop_fraction": "Maximum reference-price stop distance.",
    "reward_to_risk": "Gross initial target multiple; net cost screen is separate.",
    "min_net_reward_risk": "Minimum expected reward/risk after modeled costs.",
    "max_hold_sessions": "Sessions after entry session; 16–20 needs extension acknowledgement.",
    "extended_hold_ack": "Explicit exception to default 15-session maximum, never above 20.",
    "earnings_exit_enabled": "Exit owned positions ahead of known imminent next-session earnings.",
    "technical_exit_enabled": "Optional two daily closes below SMA20 exit.",
    "min_price": "Minimum price; cannot be below $5.",
    "min_daily_dollar_volume": "Median of prior 20 daily price×volume observations on selected feed; not consolidated if IEX.",
    "min_daily_bars": "Daily indicator/liquidity warmup.",
    "min_intraday_coverage": "Required observed regular-session slot fraction.",
    "max_spread_bps": "Maximum verified quote spread, basis points.",
    "slippage_bps": "Modeled slippage per side, minimum 10 bps.",
    "spread_bps": "Modeled full spread; replay charges half each side.",
    "fee_per_share": "Modeled per-share fee each side.",
    "no_chase_fraction": "Maximum entry extension over signal reference.",
    "signal_ttl_minutes": "Signal life after candle close.",
    "max_quote_age_seconds": "Age limit for quote at final risk check.",
    "entry_close_buffer_minutes": "No entry this many minutes before session close.",
    "universe_size": "Requested selected count, 1–1000; actual coverage is reported.",
    "universe_mode": "dynamic discovery or explicit static symbols.",
    "symbols": "Comma-separated static/replay names; dynamic universe ignores this restriction.",
    "sector_map_path": "Optional local JSON mapping ticker to verified sector; unclassified names use UNKNOWN.",
    "batch_size": "Symbols per market-data request batch.",
    "request_interval_seconds": "Minimum interval per provider client; calls are sequential.",
    "request_timeout_seconds": "Bounded provider request timeout.",
    "data_feed": "iex or sip; no silent fallback.",
    "iex_execution_ack": "IEX execution requires I_ACCEPT_IEX_LIMITATIONS.",
    "shortlist_size": "Maximum distinct symbols receiving candidate AI review; minimum configurable 5.",
    "max_entries_per_scan": "Maximum executed entries, never above 3.",
    "news_max_age_hours": "Maximum news age from publication; updates after decision time rejected.",
    "finnhub_api_key": "Optional licensed calendar credential; secret. Required for default event-risk checks.",
    "require_event_calendar": "Missing upcoming earnings coverage blocks entries by default.",
    "earnings_blackout_sessions": "No new entry within this many sessions of scheduled earnings.",
    "event_discovery_limit": "Recent-earnings symbols with release-news retrieval per scan; audited coverage cap.",
    "macro_calendar_enabled": "Optional Finnhub economic-calendar entitlement; strict timestamp checks.",
    "ai_enabled": "Allow programmatic billed AI review; false cannot authorize entries.",
    "ai_max_calls_daily": "UTC daily call cap, durable and pessimistic after timeout.",
    "ai_daily_budget_usd": "Estimated UTC daily spend cap; also set provider-side limits.",
    "ai_input_usd_per_million": "Uncached input rate for GPT-5.4; update when changing model or pricing.",
    "ai_output_usd_per_million": "Output rate for GPT-5.4, including reasoning tokens; update when changing model or pricing.",
    "ai_max_input_bytes": "Reject oversized evidence packages; never silently trim contradictory articles.",
    "ai_max_output_tokens": "Response token upper bound.",
    "ai_min_interval_seconds": "Durable call throttle.",
    "state_path": "Local persistent SQLite path; all local invocations must use the same path.",
    "database_url": "Persistent PostgreSQL connection string; secret; required for scheduled AI/execution.",
    "github_actions": "Automatically true on GitHub runners; do not override there.",
    "sheets_enabled": "Explicitly enable writes to dedicated Alpaca Paper Fills tab only.",
    "google_sheet_id": "Target spreadsheet ID, not URL; never writes existing simulated portfolio tabs.",
    "google_sa_json_base64": "Base64 service-account JSON, secret; share target sheet with that service account.",
    "alert_webhook_url": "Optional HTTPS incoming webhook destination; secret.",
    "alert_webhook_kind": "discord uses content; slack uses text.",
    "reports_dir": "Local audit/replay/report export directory; artifacts may contain private trading history.",
    "smoke_test_notional_cap": "Paper smoke-test cap, maximum $100; all normal approval/risk gates still apply.",
}
rows = [
    "# Configuration",
    "",
    "Settings use environment variables first, then a private `.env`, then the defaults below.",
    "No secrets are embedded in this reference. Unknown `.env` keys are ignored for compatibility.",
    "Malformed typed values and unsafe endpoints fail closed. Run `python -m trading_bot validate-config`.",
    "",
    "| Variable | Default | Meaning |",
    "|---|---|---|",
]
env = [
    "# Safe defaults. Copy to .env and edit privately; .env is ignored by Git.",
    "# Never enable execution before reviewing TEST_REPORT.md and OPERATIONS.md.",
]
for name, field in Settings.model_fields.items():
    value = field.default
    if isinstance(value, SecretStr):
        value = ""
    if isinstance(value, Path):
        value = str(value)
    encoded = json.dumps(value) if isinstance(value, str) else str(value).lower()
    env.append(f"{name.upper()}={encoded}")
    rows.append(f"| `{name.upper()}` | `{encoded}` | {notes[name]} |")
rows += [
    "",
    "## Usage notes",
    "",
    "Local configuration can use every variable above. The workflow wires common settings and all secrets explicitly; add another named variable to its `env` mapping when changing a less common default on GitHub. GitHub repository variables are not automatically OS environment variables.",
    "",
    "GPT-5.4 is the preferred model. Its standard short-context rates were verified on 2026-11-22: $2.50 per million input tokens and $15 per million output tokens (https://developers.openai.com/api/docs/models/gpt-5.4). Cost reservations remain conservative: they estimate input tokens from bytes, include schema overhead and the output ceiling, and are not refunded. The $1 daily budget can therefore stop before 30 calls. Sharing allowances may reduce the provider bill but do not disable this local guard. The request does not set reasoning effort; GPT-5.4 currently defaults to none. Revalidate output limits, latency and rates before changing model or reasoning settings.",
    "",
    "Paper execution gates are cumulative: mode `paper-execute`, `PAPER_ORDERS_ENABLED=true`, `PAPER_ONLY_ACK=YES_PAPER_ONLY`, `EXECUTION_VALIDATED_ACK=PAPER_CHECKS_REVIEWED`, valid paper credentials, durable state, and all normal technical/AI/risk gates. IEX additionally requires `IEX_EXECUTION_ACK=I_ACCEPT_IEX_LIMITATIONS`. None are set by this delivery.",
    "",
    "Do not disable `REQUIRE_EVENT_CALENDAR` just to generate trades. Disabling it is an explicit research/operational choice with missing event-risk protection. Keep it true for guarded execution.",
    "",
    "The legacy `ENABLE_TIME_EXITS`, `TIME_EXIT_CALENDAR_DAYS`, `ENABLE_TRAILING_STOPS` and `EARNINGS_DATES_JSON` switches no longer control the supported application. Time exits now default to 15 sessions. The preserved low-level legacy replay still accepts its explicit calendar-day argument for comparison.",
]
Path(".env.example").write_text("\n".join(env) + "\n")
Path("CONFIGURATION.md").write_text("\n".join(rows) + "\n")
