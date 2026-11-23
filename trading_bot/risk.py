from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from strategy import shares_for_trade

from .ai import validate_approval
from .calendar import entry_session
from .evidence import imminent_earnings
from .models import Approval, Candidate, OrderPlan, Portfolio, Quote


def plan_order(
    candidate: Candidate,
    approval: Approval,
    news,
    quote: Quote,
    portfolio: Portfolio,
    settings,
    now,
    events=(),
    calendar_ok=False,
    correlations=None,
) -> OrderPlan:
    validate_approval(approval, candidate, news, now, settings, events=events)
    qty = size_candidate(
        candidate, quote, portfolio, settings, now, events, calendar_ok, correlations
    )
    return OrderPlan(
        candidate=candidate,
        approval=approval,
        quantity=qty,
        limit_price=candidate.entry_high,
        validated_at=now,
    )


def size_candidate(
    candidate: Candidate,
    quote: Quote,
    portfolio: Portfolio,
    settings,
    now,
    events=(),
    calendar_ok=False,
    correlations=None,
) -> int:
    """Shared deterministic gates for execution and research. No order side effects."""
    if settings.kill_switch:
        raise ValueError("Kill switch blocks new entries")
    if not portfolio.safe:
        raise ValueError("Portfolio reconciliation unsafe")
    if not entry_session(now, settings.entry_close_buffer_minutes):
        raise ValueError("Outside entry session")
    if not candidate.signal_at <= now < candidate.expires_at:
        raise ValueError("Expired technical signal")
    if settings.require_event_calendar and not calendar_ok:
        raise ValueError("Upcoming earnings calendar unavailable")
    if imminent_earnings(candidate.symbol, events, now, settings.earnings_blackout_sessions):
        raise ValueError("Imminent earnings binary risk")
    if any(
        e.kind == "macro"
        and e.known_at <= now
        and str(e.facts.get("impact", "")).lower() == "high"
        and abs((e.event_at - now).total_seconds()) <= 1800
        for e in events
    ):
        raise ValueError("High-impact macro event within 30 minutes")
    if quote.symbol != candidate.symbol or quote.feed != settings.data_feed:
        raise ValueError("Wrong quote symbol/feed")
    if not 0 <= (now - quote.at).total_seconds() <= settings.max_quote_age_seconds:
        raise ValueError("Stale or future quote")
    if (quote.ask - quote.bid) / quote.ask * 10000 > settings.max_spread_bps:
        raise ValueError("Spread too wide")
    if not candidate.entry_low <= quote.bid <= quote.ask <= candidate.entry_high:
        raise ValueError("Price outside entry zone / no-chase ceiling")
    if candidate.symbol in portfolio.positions or candidate.symbol in portfolio.pending_symbols:
        raise ValueError("Existing position or pending order")
    if portfolio.sector_counts.get(candidate.sector, 0) >= settings.max_per_sector:
        raise ValueError("Sector concentration limit")
    correlations = correlations or {}
    if any(s not in correlations for s in portfolio.positions):
        raise ValueError("Missing portfolio correlation evidence")
    if (
        sum(correlations[s] >= settings.correlation_limit for s in portfolio.positions)
        >= settings.max_correlated_positions
    ):
        raise ValueError("Correlated-position limit")
    # Size at the hard limit ceiling, not the lower reference/quote price.
    price = candidate.entry_high
    costs = (
        price * (2 * settings.slippage_bps + settings.spread_bps) / 10000
        + 2 * settings.fee_per_share
    )
    if (candidate.target - price - costs) / (
        price - candidate.stop + costs
    ) < settings.min_net_reward_risk:
        raise ValueError("Reward/risk after costs insufficient")
    limits = settings.limits()
    limits = replace(
        limits,
        risk_per_trade=limits.risk_per_trade * Decimal(str(candidate.regime.size_multiplier)),
    )
    qty = shares_for_trade(
        Decimal(str(price)),
        Decimal(str(candidate.stop)),
        Decimal(str(portfolio.equity)),
        Decimal(str(min(portfolio.cash, portfolio.buying_power))),
        Decimal(str(portfolio.exposure)),
        Decimal(str(portfolio.stop_risk)),
        len(portfolio.positions) + len(portfolio.pending_symbols),
        limits,
    )
    qty = min(qty, int(settings.max_order_notional // price))
    if qty <= 0:
        raise ValueError("Portfolio, cash or risk limit exceeded")
    return qty
