"""Single-account, next-bar-open, conservative OHLC historical replay.

Illustrative only: IEX data, no spread/market impact or corporate-action model.
This cannot establish real-world strategy profitability.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import csv
import json

import pandas as pd

from strategy import Limits, Signal, candidate_at, shares_for_trade


@dataclass
class ReplayPosition:
    symbol: str
    qty: int
    opened_at: pd.Timestamp
    entry: float
    stop: float
    target: float
    setup: str


def run_replay(intraday: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
               output_dir: str = "reports", starting_cash: float = 25000.0,
               limits: Limits = Limits(), slippage: float = 0.001) -> dict:
    """For each symbol, signals at bar close; buy only at NEXT available bar open.

    Within a bar hitting both stop and target, assume stop first.
    No re-entry on the same bar as an exit. Long only.
    """
    indexes = {sym: {t: idx for idx, t in enumerate(df.index)}
               for sym, df in intraday.items()}
    times = sorted(set().union(*(set(x.index) for x in intraday.values())))
    positions: dict[str, ReplayPosition] = {}
    queued: dict[str, tuple[Signal, pd.Timestamp]] = {}
    cash = float(starting_cash)
    trades: list[dict] = []
    total_entries = 0
    initial_time = times[0] if times else None

    for when in times:
        exited_this_bar: set[str] = set()
        # Process exits before entries; no look-ahead into subsequent bars.
        for sym, pos in list(positions.items()):
            i = indexes[sym].get(when)
            if i is None:
                continue
            row = intraday[sym].iloc[i]
            exit_px, why = None, None
            if float(row["low"]) <= pos.stop:
                exit_px = min(float(row["open"]), pos.stop) * (1 - slippage)
                why = "stop (conservative if both hit)"
            elif float(row["high"]) >= pos.target:
                exit_px = pos.target * (1 - slippage)
                why = "target"
            elif (when - pos.opened_at).days >= 21:
                exit_px = float(row["open"]) * (1 - slippage)
                why = "time exit"
            if exit_px is not None:
                cash += pos.qty * exit_px
                trades.append({"symbol": sym, "setup": pos.setup,
                               "entry_time": pos.opened_at.isoformat(),
                               "exit_time": when.isoformat(), "qty": pos.qty,
                               "entry": round(pos.entry, 4), "exit": round(exit_px, 4),
                               "pnl": round((exit_px - pos.entry) * pos.qty, 2),
                               "reason": why})
                del positions[sym]
                exited_this_bar.add(sym)

        for sym, (signal, detected) in list(queued.items()):
            i = indexes[sym].get(when)
            if i is None or when <= detected:
                continue
            del queued[sym]
            if sym in positions or sym in exited_this_bar:
                continue
            row = intraday[sym].iloc[i]
            price = float(row["open"]) * (1 + slippage)
            if price <= signal.stop or price > signal.reference * 1.01:
                continue
            target = price + (price - signal.stop) * float(limits.reward_to_risk)
            exposure = sum(p.qty * p.entry for p in positions.values())
            risk = sum(p.qty * max(0, p.entry - p.stop) for p in positions.values())
            # Simulate account equity conservatively via cash + entry book value.
            equity = cash + exposure
            qty = shares_for_trade(Decimal(str(price)), Decimal(str(signal.stop)),
                                   Decimal(str(equity)), Decimal(str(cash)),
                                   Decimal(str(exposure)), Decimal(str(risk)),
                                   len(positions), limits)
            if qty <= 0:
                continue
            cash -= qty * price
            positions[sym] = ReplayPosition(sym, qty, when, price, signal.stop,
                                            target, signal.setup)
            total_entries += 1
            # An entry may stop out on its very first bar. Never skip this risk.
            # We know OHLC for this bar only for retrospective replay.
            first_exit, why = None, None
            if float(row["low"]) <= signal.stop:
                first_exit = min(float(row["open"]), signal.stop) * (1 - slippage)
                why = "entry-bar stop"
            elif float(row["high"]) >= target:
                first_exit = target * (1 - slippage)
                why = "entry-bar target"
            if first_exit is not None:
                cash += qty * first_exit
                trades.append({"symbol": sym, "setup": signal.setup,
                               "entry_time": when.isoformat(), "exit_time": when.isoformat(),
                               "qty": qty, "entry": round(price, 4),
                               "exit": round(first_exit, 4),
                               "pnl": round((first_exit - price) * qty, 2),
                               "reason": why})
                del positions[sym]
                exited_this_bar.add(sym)

        # Compute today's signals from just-completed bar; next bar fills only.
        for sym, idxmap in indexes.items():
            i = idxmap.get(when)
            if i is None or sym in positions or sym in queued or sym in exited_this_bar:
                continue
            signal = candidate_at(sym, intraday[sym], daily[sym], i, limits=limits)
            if signal is not None:
                queued[sym] = (signal, when)

    # Mark remaining positions to last available close, not as realized P&L.
    marked = cash + sum(p.qty * float(intraday[sym].iloc[-1]["close"])
                        for sym, p in positions.items())
    completed = sum(float(t["pnl"]) for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    result = {"observed_from": str(initial_time), "observed_to": str(times[-1]) if times else "",
              "starting_cash": starting_cash, "closed_trades": len(trades),
              "entries": total_entries, "wins": wins,
              "win_rate_closed": round(wins / len(trades), 4) if trades else None,
              "realized_pnl": round(completed, 2), "marked_equity": round(marked, 2),
              "open_positions": list(positions),
              "assumptions": "IEX-only OHLC; next-bar open; 0.1% slippage per side; stop-first on same-bar ambiguity; no fees"}
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / "replay_trades.csv").open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["symbol", "setup", "entry_time", "exit_time",
                                           "qty", "entry", "exit", "pnl", "reason"])
        w.writeheader()
        w.writerows(trades)
    (dest / "replay_summary.json").write_text(json.dumps(result, indent=2))
    return result
