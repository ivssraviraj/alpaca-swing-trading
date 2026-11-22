"""Pure signal and sizing logic. No API calls or order side effects.

IEX historical data is a limited-exchange sample, NOT consolidated market data.
This is an unproven example strategy, not a faithful port of prior ChatGPT scans.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class Signal:
    symbol: str
    bar_time: datetime
    setup: str
    reference: float
    stop: float
    target: float
    volume_ratio: float


@dataclass(frozen=True)
class Limits:
    strategy_capital: Decimal = Decimal("25000")
    risk_per_trade: Decimal = Decimal("0.0075")
    max_total_risk: Decimal = Decimal("0.03")
    max_position_fraction: Decimal = Decimal("0.12")
    max_total_exposure: Decimal = Decimal("0.60")
    max_positions: int = 4
    min_stop_fraction: Decimal = Decimal("0.015")
    max_stop_fraction: Decimal = Decimal("0.07")
    reward_to_risk: Decimal = Decimal("2.0")


def current_reg_session(frame: pd.DataFrame) -> pd.DataFrame:
    """Restrict to regular NYSE session, excluding 16:00 and later."""
    if frame.empty:
        return frame
    if frame.index.tz is None:
        raise ValueError("Index must be timezone aware")
    x = frame.sort_index().copy()
    local = x.index.tz_convert("America/New_York")
    clock = local.hour * 60 + local.minute
    return x[(local.dayofweek < 5) & (clock >= 570) & (clock < 960)]


def bar_is_fresh(bar_start: datetime, now: datetime, max_minutes: float = 20) -> bool:
    """A bar timestamp is its START. Never trade an unfinished or stale candle."""
    if bar_start.tzinfo is None or now.tzinfo is None:
        return False
    age = (now - bar_start).total_seconds() / 60 - 5.0
    return 0.5 <= age <= max_minutes


def _daily_history_before(daily: pd.DataFrame, current_bar: datetime) -> pd.DataFrame:
    # Exclude current trading day's partially populated daily candle.
    if daily.empty:
        return daily
    day = current_bar.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
    local_day = daily.index.tz_convert("America/New_York").date
    return daily[local_day < day]


def candidate_at(symbol: str, intraday: pd.DataFrame, daily: pd.DataFrame,
                 i: int, *, limits: Limits = Limits()) -> Optional[Signal]:
    """Signal from information available by the CLOSE of bar i.

    Entry for replay is on the NEXT bar; live is after the bar is complete.
    Setup A: 20-bar high breakout + IEX volume expansion + daily uptrend.
    Setup B: 5m EMA20 recovery in EMA20>EMA50 uptrend + daily uptrend.
    """
    if i < 60 or i >= len(intraday):
        return None
    bar_time = intraday.index[i].to_pydatetime()
    history = _daily_history_before(daily, bar_time)
    if len(history) < 25:
        return None
    closes = history["close"].astype(float)
    sma20 = closes.tail(20).mean()
    sma5 = closes.tail(5).mean()
    if sma20 <= 0 or not (closes.iloc[-1] > sma20 * 0.98 and sma5 > sma20):
        return None

    prev_d = history.tail(16).astype(float)
    prior_close = prev_d["close"].shift(1)
    tr = pd.concat([
        prev_d["high"] - prev_d["low"],
        (prev_d["high"] - prior_close).abs(),
        (prev_d["low"] - prior_close).abs(),
    ], axis=1).max(axis=1).iloc[1:]
    atr = float(tr.mean())
    if not (atr > 0):
        return None

    recent = intraday.iloc[i-60:i+1]
    last = recent.iloc[-1]
    prev20 = recent.iloc[-21:-1]
    price = float(last["close"])
    avg_vol = float(prev20["volume"].mean())
    if price < 5 or avg_vol <= 0 or float(last["volume"]) <= 0:
        return None
    vol_ratio = float(last["volume"]) / avg_vol

    breakout = price > float(prev20["high"].max()) and vol_ratio >= 1.25
    close = recent["close"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    pullback = (ema20.iloc[-1] > ema50.iloc[-1]
                and close.iloc[-2] <= ema20.iloc[-2]
                and price > ema20.iloc[-1]
                and price > float(close.iloc[-2])
                and vol_ratio >= 0.85)
    if not (breakout or pullback):
        return None

    # Daily ATR produces swing-scale stops; cap to avoid pathological risk.
    stop_distance = max(1.5 * atr, price * float(limits.min_stop_fraction))
    if stop_distance / price > float(limits.max_stop_fraction):
        return None
    stop = float(Decimal(str(price - stop_distance)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN))
    target = float(Decimal(str(price + stop_distance * float(limits.reward_to_risk))).quantize(
        Decimal("0.01"), rounding=ROUND_UP))
    if stop <= 0 or stop >= price or target <= price:
        return None
    return Signal(symbol, bar_time, "breakout" if breakout else "pullback",
                  price, stop, target, round(vol_ratio, 3))


def shares_for_trade(reference: Decimal, stop: Decimal, equity: Decimal,
                     available_cash: Decimal, existing_exposure: Decimal,
                     existing_risk: Decimal, current_positions: int,
                     limits: Limits = Limits()) -> int:
    """Long-only whole-share size, no margin; clamp to strategy capital."""
    if (reference <= 0 or stop <= 0 or reference <= stop or equity <= 0
            or available_cash <= 0 or current_positions >= limits.max_positions):
        return 0
    capital = min(equity, limits.strategy_capital)
    per_share = reference - stop
    if not (limits.min_stop_fraction <= per_share / reference <= limits.max_stop_fraction):
        return 0
    allowed_risk = min(capital * limits.risk_per_trade,
                       capital * limits.max_total_risk - existing_risk)
    allowed_exposure = min(capital * limits.max_position_fraction,
                           capital * limits.max_total_exposure - existing_exposure,
                           available_cash * Decimal("0.99"))
    if allowed_risk <= 0 or allowed_exposure <= 0:
        return 0
    # Additional 0.5% buffer for market-order slippage and spread.
    risk_unit = per_share + reference * Decimal("0.005")
    risk_qty = (allowed_risk / risk_unit).to_integral_value(rounding=ROUND_DOWN)
    cash_qty = (allowed_exposure / (reference * Decimal("1.01"))).to_integral_value(rounding=ROUND_DOWN)
    return max(0, int(min(risk_qty, cash_qty)))
