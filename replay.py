"""Chronological portfolio replay for diagnostics only: no broker order calls.

Uses IEX OHLC, simplified next-bar open entry/exit, and explicit data checks.
Not a complete fill simulator or evidence of a statistically valid strategy.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

from data_audit import next_bar_eligible, session_coverage
from strategy import Limits, Signal, candidate_at, shares_for_trade
from trading_bot.diagnostics import emit, traced


@dataclass
class ReplayPosition:
    symbol: str
    qty: int
    opened_at: pd.Timestamp
    entry: float
    stop: float
    target: float
    setup: str


@traced("legacy.replay")
def run_replay(
    intraday: dict[str, pd.DataFrame],
    daily: dict[str, pd.DataFrame],
    output_dir: str = "reports",
    starting_cash: float = 25000.0,
    limits: Limits = Limits(),
    slippage: float = 0.001,
    time_exit_calendar_days: int | None = None,
    max_hold_sessions: int | None = None,
    spread_bps: float = 0,
    fee_per_share: float = 0,
    signal_factory=None,
) -> dict:
    """Buy at eligible next 5-minute bar open, never at signal-bar close.

    IMPORTANT: Default time exits are OFF to match live paper's default.
    An OHLC bar crossing both exit levels is recorded as a stop first.
    This does not model order book liquidity, corporate actions, or extended-hours gaps.
    """
    if time_exit_calendar_days is not None and time_exit_calendar_days < 7:
        raise ValueError("time exit must be at least 7 calendar days")
    if not (0 <= slippage <= 0.05):
        raise ValueError("invalid slippage")
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    quality = session_coverage(intraday, str(dest))

    indexes = {sym: {t: idx for idx, t in enumerate(df.index)} for sym, df in intraday.items()}
    times = sorted(set().union(*(set(x.index) for x in intraday.values())))
    positions: dict[str, ReplayPosition] = {}
    queued: dict[str, tuple[Signal, pd.Timestamp]] = {}
    latest_prices: dict[str, float] = {}
    cash = float(starting_cash)
    trades: list[dict] = []
    equity_curve: list[dict] = []
    initial_time = times[0] if times else None
    total_entries = 0
    stale_skips = 0
    signal_count = 0
    from trading_bot.calendar import held_sessions

    execution_cost = slippage + spread_bps / 20000

    for when in times:
        exited_this_bar: set[str] = set()
        for sym, frame in intraday.items():
            idx = indexes[sym].get(when)
            if idx is not None:
                # At entry sizing time only this bar's OPEN is known.
                latest_prices[sym] = float(frame.iloc[idx]["open"])

        # Exits for positions held at the start of this bar.
        for sym, pos in list(positions.items()):
            i = indexes[sym].get(when)
            if i is None:
                continue
            row = intraday[sym].iloc[i]
            low, high = float(row["low"]), float(row["high"])
            exit_px, why = None, None
            if low <= pos.stop:
                exit_px = min(float(row["open"]), pos.stop) * (1 - execution_cost) - fee_per_share
                why = "stop (both levels touched; stop-first)" if high >= pos.target else "stop"
            elif high >= pos.target:
                exit_px = pos.target * (1 - execution_cost) - fee_per_share
                why = "target"
            elif (
                max_hold_sessions is not None
                and held_sessions(pos.opened_at, when) >= max_hold_sessions
            ) or (
                time_exit_calendar_days is not None
                and (when - pos.opened_at).total_seconds() >= time_exit_calendar_days * 86400
            ):
                exit_px = float(row["open"]) * (1 - execution_cost) - fee_per_share
                why = "time exit"
            if exit_px is not None:
                cash += pos.qty * exit_px
                trades.append(
                    {
                        "symbol": sym,
                        "setup": pos.setup,
                        "entry_time": pos.opened_at.isoformat(),
                        "exit_time": when.isoformat(),
                        "qty": pos.qty,
                        "entry": round(pos.entry, 4),
                        "exit": round(exit_px, 4),
                        "pnl": round((exit_px - pos.entry) * pos.qty, 2),
                        "reason": why,
                    }
                )
                del positions[sym]
                exited_this_bar.add(sym)

        # Entry orders queued by the PREVIOUS signal bar.
        for sym, (signal, detected) in list(queued.items()):
            i = indexes[sym].get(when)
            if i is None or when <= detected:
                continue
            del queued[sym]
            if not next_bar_eligible(detected, when):
                stale_skips += 1
                continue
            if sym in positions or sym in exited_this_bar:
                continue
            row = intraday[sym].iloc[i]
            price = float(row["open"]) * (1 + execution_cost) + fee_per_share
            if price <= signal.stop or price > signal.reference * 1.01:
                continue
            target = signal.target  # Never move a target using a favorable later fill.
            exposure = sum(p.qty * latest_prices.get(p.symbol, p.entry) for p in positions.values())
            risk = sum(p.qty * max(0, p.entry - p.stop) for p in positions.values())
            equity = cash + exposure
            qty = shares_for_trade(
                Decimal(str(price)),
                Decimal(str(signal.stop)),
                Decimal(str(equity)),
                Decimal(str(cash)),
                Decimal(str(exposure)),
                Decimal(str(risk)),
                len(positions),
                limits,
            )
            if qty <= 0:
                continue
            cash -= qty * price
            positions[sym] = ReplayPosition(
                sym, qty, when, price, signal.stop, target, signal.setup
            )
            total_entries += 1
            # A stop/target can hit in the entry bar. Stop-first on ambiguity.
            first_exit, why = None, None
            low, high = float(row["low"]), float(row["high"])
            if low <= signal.stop:
                first_exit = (
                    min(float(row["open"]), signal.stop) * (1 - execution_cost) - fee_per_share
                )
                why = "entry-bar stop (both levels touched)" if high >= target else "entry-bar stop"
            elif high >= target:
                first_exit = target * (1 - execution_cost) - fee_per_share
                why = "entry-bar target"
            if first_exit is not None:
                cash += qty * first_exit
                trades.append(
                    {
                        "symbol": sym,
                        "setup": signal.setup,
                        "entry_time": when.isoformat(),
                        "exit_time": when.isoformat(),
                        "qty": qty,
                        "entry": round(price, 4),
                        "exit": round(first_exit, 4),
                        "pnl": round((first_exit - price) * qty, 2),
                        "reason": why,
                    }
                )
                del positions[sym]
                exited_this_bar.add(sym)

        # Compute signals using only the just-completed bar. Never trade them now.
        for sym, idxmap in indexes.items():
            i = idxmap.get(when)
            if i is None or sym in positions or sym in queued or sym in exited_this_bar:
                continue
            signal = (
                signal_factory(sym, intraday[sym], daily[sym], i)
                if signal_factory
                else candidate_at(sym, intraday[sym], daily[sym], i, limits=limits)
            )
            if signal is not None:
                signal_count += 1
                queued[sym] = (signal, when)

        for sym, frame in intraday.items():
            i = indexes[sym].get(when)
            if i is not None:
                latest_prices[sym] = float(frame.iloc[i]["close"])
        equity = cash + sum(p.qty * latest_prices.get(sym, p.entry) for sym, p in positions.items())
        equity_curve.append(
            {
                "timestamp_utc": when.isoformat(),
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "open_positions": len(positions),
            }
        )

    marked = equity_curve[-1]["equity"] if equity_curve else cash
    high_mark = starting_cash
    max_drawdown = 0.0
    for point in equity_curve:
        high_mark = max(high_mark, point["equity"])
        max_drawdown = max(max_drawdown, (high_mark - point["equity"]) / high_mark)
    completed = sum(float(t["pnl"]) for t in trades)
    wins = [float(t["pnl"]) for t in trades if float(t["pnl"]) > 0]
    losses = [float(t["pnl"]) for t in trades if float(t["pnl"]) <= 0]
    by_setup = {}
    for row in trades:
        setup = row["setup"]
        metrics = by_setup.setdefault(setup, {"trades": 0, "wins": 0, "pnl": 0.0})
        metrics["trades"] += 1
        metrics["wins"] += int(row["pnl"] > 0)
        metrics["pnl"] += row["pnl"]
    for metrics in by_setup.values():
        metrics["pnl"] = round(metrics["pnl"], 2)

    result = {
        "observed_from": str(initial_time),
        "observed_to": str(times[-1]) if times else "",
        "starting_cash": starting_cash,
        "closed_trades": len(trades),
        "entries": total_entries,
        "wins": len(wins),
        "signals": signal_count,
        "max_hold_sessions": max_hold_sessions,
        "slippage_bps": slippage * 10000,
        "spread_bps": spread_bps,
        "fee_per_share": fee_per_share,
        "win_rate_closed": round(len(wins) / len(trades), 4) if trades else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "realized_pnl": round(completed, 2),
        "marked_equity": round(marked, 2),
        "marked_return_pct": round((marked / starting_cash - 1) * 100, 2),
        "max_drawdown_pct_estimate": round(max_drawdown * 100, 2),
        "open_positions": list(positions),
        "stale_signal_skips": stale_skips,
        "time_exit_calendar_days": time_exit_calendar_days,
        "trades_by_setup": by_setup,
        "data_quality": quality,
        "data_quality_pass": bool(quality)
        and all(x.get("status") == "APPROX_OK" for x in quality.values()),
        "conclusion": "NOT VALIDATED: inspect data coverage and broader out-of-sample results before paper execution",
        "assumptions": "IEX only; next eligible bar open; 0.1% slippage each side; stop-first on same-bar ambiguity; no spread, impact, real fill latency, or corporate-action model",
    }
    with (dest / "replay_trades.csv").open("w", newline="") as fp:
        w = csv.DictWriter(
            fp,
            fieldnames=[
                "symbol",
                "setup",
                "entry_time",
                "exit_time",
                "qty",
                "entry",
                "exit",
                "pnl",
                "reason",
            ],
        )
        w.writeheader()
        w.writerows(trades)
    with (dest / "replay_equity_curve.csv").open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["timestamp_utc", "equity", "cash", "open_positions"])
        w.writeheader()
        w.writerows(equity_curve)
    (dest / "replay_summary.json").write_text(json.dumps(result, indent=2))
    emit("legacy_replay_completed", "INFO", result=result)
    return result
