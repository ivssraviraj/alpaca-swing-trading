from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from strategy import candidate_at, bar_is_fresh, shares_for_trade, current_reg_session
from replay import run_replay
from engine import order_id, managed_exposure, validate_paper_gate


def samples(bar_count=62):
    # Daily data belongs to PREVIOUS days, not today: avoids look-ahead.
    dates = pd.date_range("2026-09-01", periods=45, freq="B", tz="America/New_York")
    daily = pd.DataFrame({
        "open": [100+i*.50 for i in range(len(dates))],
        "high": [101+i*.50 for i in range(len(dates))],
        "low": [99+i*.50 for i in range(len(dates))],
        "close": [100+i*.50 for i in range(len(dates))],
        "volume": [50000]*len(dates),
    }, index=dates)
    ts = pd.date_range("2026-11-10 09:30", periods=bar_count,
                       freq="5min", tz="America/New_York")
    price = [127.0] * bar_count
    price[60] = 131.0
    if bar_count > 61:
        price[61] = 131.1
    df = pd.DataFrame({"open":price, "high":[p+.10 for p in price],
                       "low":[p-.10 for p in price], "close":price,
                       "volume":[100]*bar_count}, index=ts)
    df.iloc[60, df.columns.get_loc("volume")] = 300
    return df, daily


def test_completed_bar_only():
    now = datetime(2026, 11, 10, 15, 14, tzinfo=timezone.utc)
    assert bar_is_fresh(now - timedelta(minutes=10), now)
    assert not bar_is_fresh(now - timedelta(minutes=4), now)
    assert not bar_is_fresh(now - timedelta(minutes=40), now)


def test_signal_and_daily_filter():
    bars, daily = samples()
    s = candidate_at("AAPL", bars, daily, 60)
    assert s and s.setup == "breakout" and s.stop < s.reference < s.target
    assert candidate_at("AAPL", bars, daily.iloc[:10], 60) is None
    # A signal cannot depend on a later bar's prices.
    assert candidate_at("AAPL", bars.iloc[:61], daily, 60) == s


def test_sizing_never_exceeds_portfolio_bounds():
    qty = shares_for_trade(Decimal("100"), Decimal("97"), Decimal("100000"),
                           Decimal("100000"), Decimal(0), Decimal(0), 0)
    assert 0 < qty <= 30  # 12% of capped $25k / $100 = 30 shares
    assert shares_for_trade(Decimal("100"), Decimal("97"), Decimal("25000"),
                            Decimal("25000"), Decimal(0), Decimal(0), 4) == 0
    assert shares_for_trade(Decimal("100"), Decimal("97"), Decimal("25000"),
                            Decimal("25000"), Decimal(0), Decimal("751"), 0) == 0


def test_replay_never_fills_last_signal(tmp_path):
    bars, daily = samples(61)
    result = run_replay({"AAPL": bars}, {"AAPL": daily}, str(tmp_path))
    assert result["entries"] == 0
    bars, daily = samples(62)
    result = run_replay({"AAPL": bars}, {"AAPL": daily}, str(tmp_path))
    assert result["entries"] == 1
    assert (tmp_path / "replay_summary.json").exists()


def test_order_id_deterministic():
    bars, daily = samples()
    sig = candidate_at("AAPL", bars, daily, 60)
    assert order_id(sig) == order_id(sig)
    assert len(order_id(sig)) < 48


def test_paper_orders_locked_without_both_flags(monkeypatch):
    monkeypatch.delenv("PAPER_ORDERS_ENABLED", raising=False)
    monkeypatch.delenv("PAPER_ONLY_ACK", raising=False)
    with pytest.raises(RuntimeError, match="LOCKED"):
        validate_paper_gate()
    monkeypatch.setenv("PAPER_ORDERS_ENABLED", "true")
    with pytest.raises(RuntimeError, match="LOCKED"):
        validate_paper_gate()
    monkeypatch.setenv("PAPER_ONLY_ACK", "YES_PAPER_ONLY")
    validate_paper_gate()
    monkeypatch.setenv("KILL_SWITCH", "true")
    with pytest.raises(RuntimeError, match="LOCKED"):
        validate_paper_gate()


def test_unprotected_position_fails_closed():
    pos = SimpleNamespace(symbol="AAPL", side="long", qty="10",
                          avg_entry_price="100")
    with pytest.raises(RuntimeError, match="Unmanaged position"):
        managed_exposure([pos], [])
