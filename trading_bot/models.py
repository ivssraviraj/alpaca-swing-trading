from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def normalize_utc(cls, value):
        return value.astimezone(timezone.utc) if isinstance(value, datetime) else value


class Evidence(Model):
    title: str = Field(min_length=1)
    source: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://[^\s]+$")
    published_at: AwareDatetime


class News(Evidence):
    id: str
    symbols: list[str]
    summary: str
    updated_at: AwareDatetime
    retrieved_at: AwareDatetime
    provider: Literal["alpaca"] = "alpaca"


class Event(Model):
    id: str
    symbol: str
    kind: str
    event_at: AwareDatetime
    known_at: AwareDatetime
    source: str
    url: str
    facts: dict[str, float | str | None] = Field(default_factory=dict)


class Regime(Model):
    classification: Literal["SUPPORTIVE", "NEUTRAL", "HOSTILE"]
    size_multiplier: float
    evidence: dict[str, float | str | None]


class Candidate(Model):
    symbol: str
    company: str
    setup: str
    signal_at: AwareDatetime
    reference_at: AwareDatetime
    reference: float = Field(gt=0)
    trigger: float = Field(gt=0)
    entry_low: float = Field(gt=0)
    entry_high: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    risk_per_share: float = Field(gt=0)
    net_reward_risk: float
    max_hold_sessions: int
    volume_ratio: float
    relative_strength: float | None
    regime: Regime
    sector: str = "UNKNOWN"
    event_ids: list[str] = Field(default_factory=list)
    reasons: list[str]
    missing: list[str] = Field(default_factory=list)
    expires_at: AwareDatetime
    score: float

    @model_validator(mode="after")
    def ordered(self):
        if not self.stop < self.entry_low <= self.reference <= self.entry_high < self.target:
            raise ValueError("Invalid candidate price levels")
        if not self.signal_at <= self.reference_at < self.expires_at:
            raise ValueError("Invalid signal chronology")
        return self

    @property
    def signal_id(self) -> str:
        return "ai-" + fingerprint([self.symbol, self.setup, self.signal_at.isoformat()])[:32]


class Decision(Model):
    symbol: str
    decision: Literal["APPROVE", "REJECT", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    catalyst_type: str
    bullish_factors: list[str]
    bearish_factors: list[str]
    critical_risks: list[str]
    missing_information: list[str]
    evidence: list[Evidence]
    reasoning_summary: str = Field(min_length=1)
    invalidation_conditions: list[str]
    approval_expires_at: AwareDatetime


class Quote(Model):
    symbol: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    at: AwareDatetime
    feed: str

    @model_validator(mode="after")
    def spread(self):
        if self.ask < self.bid:
            raise ValueError("Crossed quote")
        return self


class Portfolio(Model):
    equity: float
    broker_equity: float | None = None
    strategy_marked_equity: float | None = None
    realized_pnl: float = 0
    unrealized_pnl: float = 0
    cash: float
    buying_power: float
    exposure: float
    stop_risk: float
    positions: dict[str, float]
    pending_symbols: list[str]
    sector_counts: dict[str, int]
    safe: bool
    reasons: list[str]


class Approval(Model):
    candidate_hash: str
    evidence_hash: str
    context_hash: str = ""
    decision: Decision
    evaluated_at: AwareDatetime


class OrderPlan(Model):
    candidate: Candidate
    approval: Approval
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    validated_at: AwareDatetime
