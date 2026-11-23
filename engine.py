"""Compatibility entry point; all brokerage mutations live in trading_bot.execution.

Legacy pure helpers remain importable. Technical-only execution is retired.
"""

from __future__ import annotations

import os
from datetime import timezone
from decimal import Decimal

from trading_bot.universe import text as as_text


def order_id(signal):
    return f"sw-{signal.symbol}-{signal.bar_time.astimezone(timezone.utc):%Y%m%d%H%M}"


def validate_paper_gate():
    """Legacy flag validation only; does not grant execution authority."""
    if (
        os.getenv("PAPER_ORDERS_ENABLED", "false").lower() != "true"
        or os.getenv("PAPER_ONLY_ACK") != "YES_PAPER_ONLY"
        or os.getenv("KILL_SWITCH", "false").lower() == "true"
    ):
        raise RuntimeError("PAPER ORDER LOCKED")


def managed_exposure(positions, orders):
    exposure, risk, parents = Decimal(0), Decimal(0), {}
    for p in positions:
        parent = next(
            (
                o
                for o in orders
                if o.symbol == p.symbol
                and str(o.client_order_id).startswith("sw-")
                and as_text(o.side) == "buy"
            ),
            None,
        )
        if parent is None:
            raise RuntimeError("Unmanaged position " + p.symbol)
        stop = next(
            (
                o
                for o in (parent.legs or [])
                if as_text(o.type) == "stop"
                and as_text(o.status) in {"new", "accepted", "partially_filled"}
            ),
            None,
        )
        if stop is None or Decimal(str(stop.qty)) - Decimal(str(stop.filled_qty or 0)) != Decimal(
            str(p.qty)
        ):
            raise RuntimeError("Missing protective exit " + p.symbol)
        qty, entry = Decimal(str(p.qty)), Decimal(str(p.avg_entry_price))
        exposure += qty * entry
        risk += qty * max(Decimal(0), entry - Decimal(str(stop.stop_price)))
        parents[p.symbol] = parent
    return exposure, risk, parents


if __name__ == "__main__":
    import sys

    from trading_bot.cli import main

    args = sys.argv[1:]
    if "--mode" in args:
        i = args.index("--mode")
        mode = args[i + 1]
        args[i : i + 2] = []
        args.insert(0, {"paper": "paper-execute", "report": "weekly-report"}.get(mode, mode))
    raise SystemExit(main(args or ["observe"]))
