"""Ordered, independently selectable technical setups. No broker or AI calls."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from strategy import candidate_at

from .calendar import NY, coverage, held_sessions, regular_completed
from .models import Candidate, Regime

SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC")


def prior_daily(frame, now):
    return frame[frame.index.tz_convert(NY).date < pd.Timestamp(now).tz_convert(NY).date()]


def regime(daily, now, events=(), macro_ok=False):
    evidence, votes = {}, []
    for symbol in ("SPY", "QQQ", "IWM"):
        frame = prior_daily(
            daily.get(symbol, pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))), now
        )
        if len(frame) < 50:
            evidence[symbol] = "unavailable"
            continue
        close = frame.close
        trend = float(close.iloc[-1] / close.tail(50).mean() - 1)
        votes.append(trend > 0)
        evidence[symbol + "_vs_sma50"] = trend
        evidence[symbol + "_realized_vol_20d"] = float(close.pct_change().tail(20).std() * 252**0.5)
    breadth = [
        float(f.close.iloc[-1] > f.close.tail(20).mean())
        for f in (prior_daily(x, now) for x in daily.values())
        if len(f) >= 30
    ]
    evidence["sample_breadth_above_sma20"] = (
        sum(breadth) / len(breadth) if len(breadth) >= 20 else None
    )
    evidence["breadth_sample_size"] = float(len(breadth))
    evidence["macro_calendar"] = "verified snapshot" if macro_ok else "unavailable"
    evidence["known_macro_events"] = float(
        sum(e.kind == "macro" and e.known_at <= now for e in events)
    )
    benchmark = daily.get("SPY")
    if benchmark is not None:
        for sector in SECTOR_ETFS:
            if sector in daily:
                evidence[sector + "_relative_strength_20d"] = relative_strength(
                    prior_daily(daily[sector], now), prior_daily(benchmark, now)
                )
    state = "NEUTRAL"
    if len(votes) == 3:
        state = "SUPPORTIVE" if all(votes) else "HOSTILE" if not any(votes) else "NEUTRAL"
    return Regime(
        classification=state, size_multiplier=0.5 if state == "HOSTILE" else 1.0, evidence=evidence
    )


def relative_strength(daily, benchmark):
    aligned = (
        pd.concat([daily.close.rename("stock"), benchmark.close.rename("bench")], axis=1)
        .dropna()
        .tail(21)
    )
    if len(aligned) < 21:
        return None
    return float(
        aligned.stock.iloc[-1] / aligned.stock.iloc[0]
        - aligned.bench.iloc[-1] / aligned.bench.iloc[0]
    )


def setup_flags(frame, daily, relative, earnings_recent, diagnostics=None):
    """Signals refer to the final completed bar; ordered retest uses earlier bars."""
    if len(frame) < 61 or len(daily) < 30:
        return {}
    c = frame.close.astype(float)
    last, previous = frame.iloc[-1], frame.iloc[-2]
    ema20, ema50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    vol = (
        float(last.volume / frame.volume.iloc[-21:-1].mean())
        if frame.volume.iloc[-21:-1].mean() > 0
        else 0
    )
    uptrend = daily.close.iloc[-1] > daily.close.tail(20).mean()
    breakout = last.close > frame.high.iloc[-21:-1].max() and vol >= 1.25 and uptrend
    pullback = (
        ema20.iloc[-1] > ema50.iloc[-1]
        and previous.close <= ema20.iloc[-2] < last.close
        and vol >= 0.85
        and uptrend
    )
    level = frame.high.iloc[-31:-6].max()
    earlier_break = frame.close.iloc[-6:-2] > level
    retest = bool(
        earlier_break.any()
        and previous.low <= level * 1.002
        and previous.close >= level * 0.995
        and last.close > previous.high
        and vol >= 1
        and uptrend
    )
    continuation = (
        uptrend
        and ema20.iloc[-1] > ema50.iloc[-1]
        and last.close > frame.high.iloc[-4:-1].max()
        and previous.close > ema20.iloc[-2]
        and vol >= 1.1
    )
    local_day = frame.index[-1].tz_convert(NY).date()
    session = frame[frame.index.tz_convert(NY).date == local_day]
    gap = float(session.open.iloc[0] / daily.close.iloc[-1] - 1)
    gapgo = (
        len(session) >= 4
        and gap >= 0.02
        and last.close > session.high.iloc[:3].max()
        and vol >= 1.25
    )
    recent_low = frame.low.iloc[-10:-3].min()
    stable = (
        frame.low.iloc[-3:].min() > recent_low
        and last.close > frame.high.iloc[-4:-1].max()
        and last.close > ema20.iloc[-1]
        and vol >= 1.25
    )
    if diagnostics is not None:
        diagnostics.update(
            {
                "daily_close": float(daily.close.iloc[-1]),
                "daily_sma20": float(daily.close.tail(20).mean()),
                "daily_uptrend": bool(uptrend),
                "close": float(last.close),
                "previous_close": float(previous.close),
                "ema20_5m": float(ema20.iloc[-1]),
                "ema50_5m": float(ema50.iloc[-1]),
                "volume_ratio": vol,
                "breakout_level": float(frame.high.iloc[-21:-1].max()),
                "retest_level": float(level),
                "gap_fraction": gap,
                "session_bars": len(session),
                "relative_strength_20d": relative,
                "recent_verified_earnings": bool(earnings_recent),
                "checks": {
                    "breakout_price": bool(last.close > frame.high.iloc[-21:-1].max()),
                    "breakout_volume_ge_1_25": vol >= 1.25,
                    "ema20_above_ema50": bool(ema20.iloc[-1] > ema50.iloc[-1]),
                    "pullback_reclaimed_ema20": bool(previous.close <= ema20.iloc[-2] < last.close),
                    "pullback_volume_ge_0_85": vol >= 0.85,
                    "earlier_breakout": bool(earlier_break.any()),
                    "retest_support": bool(
                        previous.low <= level * 1.002 and previous.close >= level * 0.995
                    ),
                    "retest_confirmation": bool(last.close > previous.high),
                    "continuation_price": bool(last.close > frame.high.iloc[-4:-1].max()),
                    "continuation_volume_ge_1_1": vol >= 1.1,
                    "gap_ge_2pct": gap >= 0.02,
                    "gap_opening_range_break": bool(last.close > session.high.iloc[:3].max()),
                    "reversal_stabilized": bool(stable),
                },
            }
        )
    return {
        "breakout": breakout,
        "pullback": pullback,
        "breakout_retest": retest,
        "trend_continuation": continuation,
        "relative_strength_breakout": breakout and relative is not None and relative > 0.03,
        "post_earnings_momentum": earnings_recent and breakout,
        "gap_and_go": gapgo,
        "post_earnings_drift": earnings_recent
        and continuation
        and relative is not None
        and relative > 0,
        "earnings_reversal": earnings_recent and gap <= -0.03 and stable,
    }


def scan(
    symbol,
    name,
    intraday,
    daily,
    benchmark,
    events,
    market_regime,
    settings,
    now,
    sector="UNKNOWN",
    diagnostics=None,
):
    detail = diagnostics if diagnostics is not None else {}
    detail.update(symbol=symbol, as_of=now.isoformat(), sector=sector)

    def reject(reason, **metrics):
        detail.update(outcome="rejected", first_failure=reason, **metrics)
        return []

    frame = regular_completed(intraday, now)
    daily = prior_daily(daily, now)
    detail.update(
        intraday_bars=len(frame),
        daily_bars=len(daily),
        required_intraday_bars=61,
        required_daily_bars=settings.min_daily_bars,
    )
    if len(frame) < 61 or len(daily) < settings.min_daily_bars:
        return reject("insufficient_bars")
    observed_coverage = coverage(frame)
    detail.update(coverage=observed_coverage, required_coverage=settings.min_intraday_coverage)
    if observed_coverage["fraction"] < settings.min_intraday_coverage:
        return reject("insufficient_intraday_coverage")
    last_time = frame.index[-1].to_pydatetime()
    completed = last_time + timedelta(minutes=5)
    detail.update(
        last_bar=last_time.isoformat(),
        last_completed=completed.isoformat(),
        signal_age_seconds=(now - completed).total_seconds(),
        max_signal_age_seconds=settings.signal_ttl_minutes * 60,
    )
    if not 0 <= (now - completed).total_seconds() <= settings.signal_ttl_minutes * 60:
        return reject("stale_or_future_signal")
    # Gaps in the immediate confirmation sequence are not ordered confirmation.
    if any(frame.index[-6:][1:] - frame.index[-6:][:-1] != pd.Timedelta(minutes=5)):
        return reject("recent_bar_gaps", recent_bar_times=[t.isoformat() for t in frame.index[-6:]])
    related = list(
        {
            e.id: e
            for e in sorted(events, key=lambda e: e.known_at)
            if e.symbol == symbol and e.known_at <= now and e.event_at <= now
        }.values()
    )
    earnings = [
        e for e in related if e.kind == "earnings_release" and held_sessions(e.event_at, now) <= 5
    ]
    strength = (
        relative_strength(daily, prior_daily(benchmark, now)) if not benchmark.empty else None
    )
    pattern_details = {} if diagnostics is not None else None
    flags = setup_flags(frame, daily, strength, bool(earnings), pattern_details)
    detail.update(patterns={k: bool(v) for k, v in flags.items()}, pattern_inputs=pattern_details)
    price = float(frame.close.iloc[-1])
    prior = daily.close.shift(1)
    atr = (
        pd.concat(
            [daily.high - daily.low, (daily.high - prior).abs(), (daily.low - prior).abs()], axis=1
        )
        .max(axis=1)
        .tail(14)
        .mean()
    )
    distance = max(1.5 * float(atr), price * settings.min_stop_fraction)
    detail.update(
        price=price,
        min_price=settings.min_price,
        atr=float(atr),
        stop_distance=distance,
        stop_fraction=distance / price if price else None,
        max_stop_fraction=settings.max_stop_fraction,
    )
    if price < settings.min_price or distance / price > settings.max_stop_fraction:
        return reject(
            "price_below_minimum" if price < settings.min_price else "stop_distance_exceeds_limit"
        )
    stop, target = round(price - distance, 2), round(price + settings.reward_to_risk * distance, 2)
    volume = float(frame.volume.iloc[-1] / frame.volume.iloc[-21:-1].mean())
    entry_high = round(price * (1 + settings.no_chase_fraction), 2)
    costs = (
        price * (2 * settings.slippage_bps + settings.spread_bps) / 10000
        + 2 * settings.fee_per_share
    )
    rr = (target - entry_high - costs) / (entry_high - stop + costs)
    detail.update(
        stop=stop,
        target=target,
        entry_ceiling=entry_high,
        modeled_round_trip_cost=costs,
        net_reward_risk=rr,
        min_net_reward_risk=settings.min_net_reward_risk,
    )
    if rr < settings.min_net_reward_risk:
        return reject("net_reward_risk_below_minimum")
    candidates = []
    # Preserve the original two rules when applicable, including its daily filter.
    original = candidate_at(symbol, frame, daily, len(frame) - 1, limits=settings.limits())
    detail["legacy_setup"] = original.setup if original else None
    for setup, passed in flags.items():
        if not passed or (
            setup in ("breakout", "pullback") and (not original or original.setup != setup)
        ):
            continue
        candidates.append(
            Candidate(
                symbol=symbol,
                company=name,
                setup=setup,
                signal_at=completed,
                reference_at=completed,
                reference=price,
                trigger=price,
                entry_low=round(price * 0.998, 2),
                entry_high=entry_high,
                stop=stop,
                target=target,
                risk_per_share=price - stop,
                net_reward_risk=rr,
                max_hold_sessions=settings.max_hold_sessions,
                volume_ratio=volume,
                relative_strength=strength,
                regime=market_regime,
                sector=sector,
                event_ids=sorted(e.id for e in related),
                reasons=[setup + " confirmed on ordered completed regular-session bars"],
                missing=["consensus fundamentals unavailable"]
                + (["relative strength unavailable"] if strength is None else []),
                expires_at=completed + timedelta(minutes=settings.signal_ttl_minutes),
                score=min(volume, 5) + (strength or 0) * 10 + (3 if earnings else 0),
            )
        )
    detail.update(
        outcome="signals" if candidates else "rejected",
        first_failure=None
        if candidates
        else ("legacy_setup_filter" if any(flags.values()) else "no_matching_setup"),
        signal_count=len(candidates),
        setups=[c.setup for c in candidates],
    )
    return candidates
