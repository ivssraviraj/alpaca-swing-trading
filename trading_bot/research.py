"""Chronological research sharing deterministic risk gates with paper execution."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

from .ai import validate_approval
from .calendar import coverage, held_sessions, regular_completed
from .diagnostics import emit, safe_value, traced
from .models import Approval, Candidate, Event, News, Portfolio, Quote
from .pipeline import correlations
from .risk import size_candidate
from .signals import prior_daily, regime, scan


@dataclass
class SimPosition:
    candidate: Candidate
    quantity: int
    entry: float
    opened: pd.Timestamp


@traced("research.replay")
def replay(intraday, daily, settings, mode="technical", archive=None):
    emit(
        "replay_started",
        "INFO",
        strategy=mode,
        symbols=list(intraday),
        intraday_bar_count=sum(len(f) for f in intraday.values()),
        slippage_bps=settings.slippage_bps,
        spread_bps=settings.spread_bps,
        fee_per_share=settings.fee_per_share,
        calendar_required=settings.require_event_calendar,
    )
    archive = archive or {}
    directory = settings.reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    if (mode in {"event", "ai"} and not archive) or (mode == "ai" and not archive.get("approvals")):
        result = {
            "status": "UNAVAILABLE",
            "mode": mode,
            "reason": "No point-in-time event/news/AI archive; no invented approvals",
        }
        (directory / f"{mode}_summary.json").write_text(json.dumps(result, indent=2))
        emit("replay_unavailable", "WARNING", **result)
        return result
    if not intraday:
        raise ValueError("No replay data")
    end = max(f.index[-1] for f in intraday.values() if not f.empty) + timedelta(days=1)
    intraday = {s: regular_completed(f, end) for s, f in intraday.items()}
    events = [Event.model_validate(e) for e in archive.get("events", [])]
    identities = archive.get("identities", {})
    indexes = {s: {t: i for i, t in enumerate(f.index)} for s, f in intraday.items()}
    times = sorted(set().union(*(set(f.index) for f in intraday.values())))
    queued, positions, marks = {}, {}, {}
    trades, curve, funnel = [], [], Counter()
    scan_failures, scan_examples = Counter(), {}
    cash, realized = settings.strategy_capital_usd, 0.0
    cost = (settings.slippage_bps + settings.spread_bps / 2) / 10000
    regime_cache, daily_cache = {}, {}

    def close(symbol, when, price, reason):
        nonlocal cash, realized
        pos = positions.pop(symbol)
        exit_price = price * (1 - cost) - settings.fee_per_share
        pnl = (exit_price - pos.entry) * pos.quantity
        cash += pos.quantity * exit_price
        realized += pnl
        emit(
            "replay_exit",
            symbol=symbol,
            at_bar=when,
            quantity=pos.quantity,
            price=exit_price,
            realized_pnl=pnl,
            reason=reason,
        )
        trades.append(
            dict(
                symbol=symbol,
                setup=pos.candidate.setup,
                regime=pos.candidate.regime.classification,
                sector=pos.candidate.sector,
                entry_time=pos.opened.isoformat(),
                exit_time=when.isoformat(),
                quantity=pos.quantity,
                entry=pos.entry,
                exit=exit_price,
                pnl=pnl,
                reason=reason,
                hold_sessions=held_sessions(pos.opened, when),
            )
        )

    for when in times:
        now = when.to_pydatetime()
        visible_events = [e for e in events if e.known_at <= now]
        rows = {s: intraday[s].iloc[indexes[s][when]] for s in intraday if when in indexes[s]}
        marks.update({s: float(r.open) for s, r in rows.items()})
        exited = set()
        # Time exits are known at the open; OHLC exit ordering stays conservative.
        for symbol, pos in list(positions.items()):
            if symbol not in rows:
                continue
            row, c = rows[symbol], pos.candidate
            if held_sessions(pos.opened, when) >= c.max_hold_sessions:
                close(symbol, when, float(row.open), "time")
            elif row.low <= c.stop:
                close(
                    symbol,
                    when,
                    min(float(row.open), c.stop),
                    "stop_both_touched" if row.high >= c.target else "stop",
                )
            elif row.high >= c.target:
                close(symbol, when, c.target, "target")
            else:
                continue
            exited.add(symbol)
        day = when.tz_convert("America/New_York").date()
        if day not in daily_cache:
            daily_cache[day] = {s: prior_daily(f, now) for s, f in daily.items()}
            regime_cache[day] = regime(daily, now)
        past_daily = daily_cache[day]
        entered = 0
        for symbol, (c, available) in sorted(list(queued.items()), key=lambda x: -x[1][0].score):
            if symbol not in rows or now < available:
                continue
            del queued[symbol]
            if symbol in positions or symbol in exited or entered >= settings.max_entries_per_scan:
                funnel["held_or_scan_cap"] += 1
                continue
            if now >= c.expires_at:
                funnel["expired"] += 1
                continue
            quote = Quote(
                symbol=symbol,
                bid=float(rows[symbol].open) * (1 - settings.spread_bps / 20000),
                ask=float(rows[symbol].open) * (1 + settings.spread_bps / 20000),
                at=now,
                feed=settings.data_feed,
            )
            if mode == "ai":
                record = archive.get("approvals", {}).get(c.signal_id)
                if record is None:
                    funnel["missing_historical_approval"] += 1
                    continue
                try:
                    validate_approval(
                        Approval.model_validate(record["approval"]),
                        c,
                        [News.model_validate(n) for n in record["news"]],
                        now,
                        settings,
                        historical=True,
                        events=[Event.model_validate(e) for e in record.get("events", [])],
                    )
                except (ValueError, KeyError):
                    funnel["invalid_historical_approval"] += 1
                    continue
                funnel["ai_approved"] += 1
            exposure = sum(p.quantity * marks[s] for s, p in positions.items())
            risk = sum(
                p.quantity * (max(0, marks[s] - p.candidate.stop) + marks[s] * 0.005)
                for s, p in positions.items()
            )
            p = Portfolio(
                equity=min(settings.strategy_capital_usd, cash + exposure),
                cash=cash,
                buying_power=cash,
                exposure=exposure,
                stop_risk=risk,
                positions={s: p.quantity for s, p in positions.items()},
                pending_symbols=[],
                sector_counts=dict(Counter(p.candidate.sector for p in positions.values())),
                safe=True,
                reasons=[],
            )
            calendar_ok = any(
                pd.Timestamp(s["known_at"]) <= when < pd.Timestamp(s["valid_until"])
                for s in archive.get("calendar_snapshots", [])
            )
            try:
                qty = size_candidate(
                    c,
                    quote,
                    p,
                    settings,
                    now,
                    visible_events,
                    calendar_ok,
                    correlations(symbol, past_daily, positions),
                )
            except ValueError as exc:
                funnel[str(exc)] += 1
                continue
            price = float(rows[symbol].open) * (1 + cost) + settings.fee_per_share
            if price > c.entry_high:
                funnel["limit_unfilled"] += 1
                continue
            cash -= qty * price
            positions[symbol] = SimPosition(c, qty, price, when)
            emit(
                "replay_entry", symbol=symbol, at_bar=when, quantity=qty, price=price, setup=c.setup
            )
            entered += 1
            funnel["entries"] += 1
            if rows[symbol].low <= c.stop:
                close(symbol, when, min(float(rows[symbol].open), c.stop), "entry_stop")
                exited.add(symbol)
            elif rows[symbol].high >= c.target:
                close(symbol, when, c.target, "entry_target")
                exited.add(symbol)
        marks.update({s: float(r.close) for s, r in rows.items()})
        for symbol in rows:
            if symbol in positions or symbol in queued or symbol in exited or symbol not in daily:
                continue
            i = indexes[symbol][when]
            if i < 60:
                continue
            available = now + timedelta(minutes=5, seconds=30)
            identity = identities.get(symbol, {})
            diagnostic = {}
            candidates = scan(
                symbol,
                identity.get("name", symbol),
                intraday[symbol].iloc[: i + 1].tail(150),
                daily[symbol],
                daily.get("SPY", pd.DataFrame()),
                [e for e in events if e.known_at <= available],
                regime_cache[day],
                settings,
                available,
                identity.get("sector", "UNKNOWN"),
                diagnostics=diagnostic,
            )
            reason = diagnostic.get("first_failure")
            if reason:
                scan_failures[reason] += 1
                if settings.debug_traces_enabled:
                    examples = scan_examples.setdefault(reason, [])
                    if len(examples) < 3:
                        examples.append(safe_value(diagnostic))
            funnel["technical_candidates"] += len(candidates)
            if mode == "event":
                candidates = [c for c in candidates if c.event_ids]
            if candidates:
                c = max(candidates, key=lambda c: c.score)
                queued[symbol] = c, available
                funnel["queued"] += 1
        equity = cash + sum(p.quantity * marks[s] for s, p in positions.items())
        curve.append(
            dict(
                timestamp=when.isoformat(),
                equity=equity,
                cash=cash,
                exposure=equity - cash,
                open_positions=len(positions),
            )
        )
    winners = [t["pnl"] for t in trades if t["pnl"] > 0]
    losers = [t["pnl"] for t in trades if t["pnl"] <= 0]
    peak, drawdown = settings.strategy_capital_usd, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, (peak - point["equity"]) / peak)
    marked = curve[-1]["equity"] if curve else cash

    def grouped(field):
        groups = {}
        for t in trades:
            row = groups.setdefault(t[field], {"trades": 0, "pnl": 0.0, "wins": 0})
            row["trades"] += 1
            row["pnl"] += t["pnl"]
            row["wins"] += int(t["pnl"] > 0)
        return groups

    benchmark = intraday.get("SPY")
    result = dict(
        status="COMPLETED",
        mode=mode,
        symbols=len(intraday),
        bars=sum(map(len, intraday.values())),
        observed_from=str(times[0]) if times else None,
        observed_to=str(times[-1]) if times else None,
        starting_capital=settings.strategy_capital_usd,
        entries=funnel["entries"],
        closed_trades=len(trades),
        wins=len(winners),
        win_rate=len(winners) / len(trades) if trades else None,
        realized_pnl=realized,
        unrealized_pnl=marked - settings.strategy_capital_usd - realized,
        marked_equity=marked,
        return_pct=(marked / settings.strategy_capital_usd - 1) * 100,
        avg_winner=sum(winners) / len(winners) if winners else None,
        avg_loser=sum(losers) / len(losers) if losers else None,
        profit_factor=sum(winners) / -sum(losers) if sum(losers) < 0 else None,
        expectancy=realized / len(trades) if trades else None,
        max_drawdown_pct=drawdown * 100,
        mean_hold_sessions=sum(t["hold_sessions"] for t in trades) / len(trades)
        if trades
        else None,
        median_hold_sessions=float(pd.Series([t["hold_sessions"] for t in trades]).median())
        if trades
        else None,
        max_hold_sessions=settings.max_hold_sessions,
        open_positions=list(positions),
        mean_exposure=sum(p["exposure"] / p["equity"] for p in curve) / len(curve) if curve else 0,
        by_setup=grouped("setup"),
        by_regime=grouped("regime"),
        by_symbol=grouped("symbol"),
        by_sector=grouped("sector"),
        exits=dict(Counter(t["reason"] for t in trades)),
        funnel=dict(funnel),
        scanner_rejection_counts=dict(scan_failures),
        spy_buy_hold_return=(
            float(benchmark.close.iloc[-1] / benchmark.open.iloc[0] - 1)
            if benchmark is not None and len(benchmark)
            else None
        ),
        coverage={s: coverage(f) for s, f in intraday.items()},
        slippage_bps=settings.slippage_bps,
        spread_bps=settings.spread_bps,
        fee_per_share=settings.fee_per_share,
        calendar_required=settings.require_event_calendar,
        caveats=[
            "Bar-open quotes/spreads are modeled, not observed executions; no queue/partial-fill or halt simulation",
            "30-second publication grace defers fills to the following available bar open",
            "Stops use gap-open/stop adverse fills; both levels touched uses stop-first assumption",
            "Current-universe survivorship and split-adjusted data biases; dividends excluded",
            "No profitability or statistical significance inferred from fixtures or small samples",
            "Historical AI requires contemporaneously archived evidence and matching decisions",
        ],
    )
    pd.DataFrame(trades).to_csv(directory / "replay_trades.csv", index=False)
    pd.DataFrame(curve).to_csv(directory / "replay_equity_curve.csv", index=False)
    (directory / f"{mode}_summary.json").write_text(json.dumps(result, indent=2))
    if settings.debug_traces_enabled:
        (directory / "replay_scan_examples.json").write_text(json.dumps(scan_examples, indent=2))
    emit("replay_completed", "INFO", strategy=mode, result=result)
    return result


def fixture_frames(path):
    from .providers import decode_frame

    data = json.loads(Path(path).read_text())
    return (
        {s: decode_frame(f) for s, f in data["intraday"].items()},
        {s: decode_frame(f) for s, f in data["daily"].items()},
    )


@traced("research.compare")
def compare(intraday, daily, settings, archive=None):
    results = {}
    for mode in ("technical", "event", "ai"):
        config = settings.model_copy(update={"reports_dir": settings.reports_dir / mode})
        results[mode] = replay(intraday, daily, config, mode, archive)
    results["cost_sensitivity"] = {}
    for bps in (10, 20, 30):
        config = settings.model_copy(
            update={"slippage_bps": bps, "reports_dir": settings.reports_dir / f"cost-{bps}"}
        )
        results["cost_sensitivity"][bps] = replay(intraday, daily, config, "technical", archive)
    (settings.reports_dir / "comparison.json").write_text(json.dumps(results, indent=2))
    return results
