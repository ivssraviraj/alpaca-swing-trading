"""Forward-only price markouts for rejected candidates; these are not simulated fills."""

from __future__ import annotations

from datetime import timedelta

from .calendar import held_sessions
from .models import Candidate
from .signals import prior_daily


def pending_rejections(store, now):
    unique = {}
    for row in store.events("candidate_rejected"):
        record = row["payload"]
        if "candidate" not in record:
            continue
        c = Candidate.model_validate(record["candidate"])
        if now - c.signal_at > timedelta(days=60):
            continue
        if not store.get("markout:" + c.signal_id + ":15"):
            unique[c.signal_id] = c
    return list(unique.values())[-1000:]


def capture_markouts(store, daily, now):
    for c in pending_rejections(store, now):
        if c.symbol not in daily:
            continue
        history = prior_daily(daily[c.symbol], now)
        for horizon in (5, 15):
            key = "markout:" + c.signal_id + ":" + str(horizon)
            if store.get(key):
                continue
            eligible = [
                t
                for t in history.index
                if t > c.signal_at and held_sessions(c.signal_at, t) >= horizon
            ]
            if not eligible:
                continue
            first = eligible[0]
            actual_horizon = held_sessions(c.signal_at, first)
            if actual_horizon != horizon:
                # Missing the requested observation day is not silently treated as an exact markout.
                continue
            close = float(history.loc[first, "close"])
            outcome = {
                "signal_id": c.signal_id,
                "symbol": c.symbol,
                "setup": c.setup,
                "horizon_sessions": horizon,
                "observation_at": first.isoformat(),
                "reference": c.reference,
                "observed_close": close,
                "gross_price_return": close / c.reference - 1,
                "label": "REJECTED CANDIDATE PRICE MARKOUT — NOT A TRADE OR REALIZABLE P&L",
            }
            store.audit("forward_markout", outcome, now, key=key)
            store.put(key, True)
