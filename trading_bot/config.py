from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAPER_URL = "https://paper-api.alpaca.markets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)
    apca_api_key_id: SecretStr = SecretStr("")
    apca_api_secret_key: SecretStr = SecretStr("")
    apca_api_base_url: str = PAPER_URL
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5.4"
    paper_orders_enabled: bool = False
    paper_only_ack: str = ""
    kill_switch: bool = False
    execution_validated_ack: str = ""
    strategy_capital_usd: float = Field(25000, ge=100, le=1000000)
    risk_per_trade: float = Field(0.0075, gt=0, le=0.015)
    max_total_risk: float = Field(0.03, gt=0, le=0.04)
    max_position_fraction: float = Field(0.12, gt=0, le=1)
    max_total_exposure: float = Field(0.60, gt=0, le=1)
    max_order_notional: float = Field(3000, gt=0)
    max_positions: int = Field(4, ge=1, le=20)
    max_per_sector: int = Field(2, ge=1)
    max_correlated_positions: int = Field(2, ge=1)
    correlation_limit: float = Field(0.85, gt=0, le=1)
    min_stop_fraction: float = Field(0.015, gt=0)
    max_stop_fraction: float = Field(0.07, gt=0, le=0.2)
    reward_to_risk: float = Field(2, ge=1.5)
    min_net_reward_risk: float = Field(1.5, ge=1.5)
    max_hold_sessions: int = Field(15, ge=1, le=20)
    extended_hold_ack: bool = False
    earnings_exit_enabled: bool = True
    technical_exit_enabled: bool = False
    min_price: float = Field(5, ge=5)
    min_daily_dollar_volume: float = Field(1000000, gt=0)
    min_daily_bars: int = Field(30, ge=25)
    min_intraday_coverage: float = Field(0.8, gt=0, le=1)
    max_spread_bps: float = Field(30, gt=0)
    slippage_bps: float = Field(10, ge=10)
    spread_bps: float = Field(5, ge=0)
    fee_per_share: float = Field(0, ge=0)
    no_chase_fraction: float = Field(0.005, gt=0, le=0.02)
    signal_ttl_minutes: int = Field(15, ge=1, le=20)
    max_quote_age_seconds: int = Field(60, ge=1, le=120)
    entry_close_buffer_minutes: int = Field(25, ge=10)
    universe_size: int = Field(100, ge=1, le=1000)
    universe_mode: Literal["dynamic", "static"] = "dynamic"
    symbols: str = "SPY,QQQ,IWM,AAPL,MSFT,NVDA"
    sector_map_path: str = ""
    batch_size: int = Field(50, ge=1, le=200)
    request_interval_seconds: float = Field(0.35, ge=0)
    request_timeout_seconds: float = Field(15, gt=0, le=60)
    data_feed: Literal["iex", "sip"] = "iex"
    iex_execution_ack: str = ""
    shortlist_size: int = Field(5, ge=5, le=20)
    max_entries_per_scan: int = Field(3, ge=1, le=3)
    news_max_age_hours: int = Field(120, ge=1, le=240)
    finnhub_api_key: SecretStr = SecretStr("")
    require_event_calendar: bool = True
    earnings_blackout_sessions: int = Field(2, ge=1, le=5)
    event_discovery_limit: int = Field(20, ge=1, le=100)
    macro_calendar_enabled: bool = False
    ai_enabled: bool = False
    ai_max_calls_daily: int = Field(30, ge=0)
    ai_daily_budget_usd: float = Field(1, ge=0)
    ai_input_usd_per_million: float = Field(2.5, gt=0)
    ai_output_usd_per_million: float = Field(15, gt=0)
    ai_max_input_bytes: int = Field(24000, ge=2000, le=100000)
    ai_max_output_tokens: int = Field(1500, ge=500, le=5000)
    ai_min_interval_seconds: float = Field(2, ge=0)
    state_path: Path = Path("state/trading.sqlite3")
    database_url: SecretStr = SecretStr("")
    github_actions: bool = False
    sheets_enabled: bool = False
    google_sheet_id: str = ""
    google_sa_json_base64: SecretStr = SecretStr("")
    alert_webhook_url: SecretStr = SecretStr("")
    alert_webhook_kind: Literal["discord", "slack"] = "discord"
    reports_dir: Path = Path("reports")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    debug_traces_enabled: bool = True
    smoke_test_notional_cap: float = Field(50, gt=0, le=100)

    @model_validator(mode="after")
    def safe(self):
        if self.apca_api_base_url.rstrip("/") != PAPER_URL:
            raise ValueError("Only the Alpaca PAPER endpoint is permitted")
        if self.min_stop_fraction >= self.max_stop_fraction:
            raise ValueError("Stop fractions must be ordered")
        if self.max_hold_sessions > 15 and not self.extended_hold_ack:
            raise ValueError("Holds over 15 sessions require EXTENDED_HOLD_ACK=true")
        if self.max_total_risk < self.risk_per_trade:
            raise ValueError("Aggregate risk must cover per-trade risk")
        return self

    def require_broker_credentials(self):
        if (
            not self.apca_api_key_id.get_secret_value()
            or not self.apca_api_secret_key.get_secret_value()
        ):
            raise ValueError("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY; configure privately")

    def execution_gate(self, mode: str):
        if mode != "paper-execute" or not self.paper_orders_enabled:
            raise ValueError("Paper execution disabled")
        if self.paper_only_ack != "YES_PAPER_ONLY":
            raise ValueError("PAPER_ONLY_ACK=YES_PAPER_ONLY required")
        if self.execution_validated_ack != "PAPER_CHECKS_REVIEWED":
            raise ValueError("EXECUTION_VALIDATED_ACK=PAPER_CHECKS_REVIEWED required")
        if self.github_actions and not self.database_url.get_secret_value():
            raise ValueError("Scheduled execution requires persistent DATABASE_URL")
        self.require_broker_credentials()

    def limits(self):
        from decimal import Decimal

        from strategy import Limits

        return Limits(
            strategy_capital=Decimal(str(self.strategy_capital_usd)),
            risk_per_trade=Decimal(str(self.risk_per_trade)),
            max_total_risk=Decimal(str(self.max_total_risk)),
            max_position_fraction=Decimal(str(self.max_position_fraction)),
            max_total_exposure=Decimal(str(self.max_total_exposure)),
            max_positions=self.max_positions,
            min_stop_fraction=Decimal(str(self.min_stop_fraction)),
            max_stop_fraction=Decimal(str(self.max_stop_fraction)),
            reward_to_risk=Decimal(str(self.reward_to_risk)),
        )
