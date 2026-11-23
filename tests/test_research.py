import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.research import compare, fixture_frames, replay

from .helpers import NOW, cfg, frames

FIXTURES = Path(__file__).parent / "fixtures"


def test_replay_shared_risk_delay_costs_and_cash_reconcile(tmp_path):
    intra, daily = fixture_frames(FIXTURES / "market.json")
    archive = json.loads((FIXTURES / "events.json").read_text())
    result = replay(intra, daily, cfg(tmp_path), archive=archive)
    assert result["entries"] == 2 and result["closed_trades"] == 2
    assert result["wins"] == 1 and not result["open_positions"]
    assert result["marked_equity"] == pytest.approx(25000 + result["realized_pnl"])
    trades = pd.read_csv(tmp_path / "reports/replay_trades.csv")
    assert all(
        pd.to_datetime(trades.entry_time, utc=True)
        >= pd.Timestamp(NOW) + timedelta(minutes=4, seconds=30)
    )
    assert set(trades.reason) == {"target", "stop"}
    higher_cost = replay(intra, daily, cfg(tmp_path, slippage_bps=20), archive=archive)
    assert higher_cost["realized_pnl"] < result["realized_pnl"]


def test_no_fabricated_historical_ai(tmp_path):
    intra, daily = fixture_frames(FIXTURES / "market.json")
    assert replay(intra, daily, cfg(tmp_path), "ai")["status"] == "UNAVAILABLE"
    result = replay(intra, daily, cfg(tmp_path), "ai", {"events": []})
    assert result["status"] == "UNAVAILABLE"


def test_historical_event_published_later_cannot_select_candidate(tmp_path):
    intra, daily = fixture_frames(FIXTURES / "market.json")
    event = dict(
        id="future",
        symbol="AAPL",
        kind="earnings_release",
        event_at="2026-11-11T12:00:00Z",
        known_at="2026-11-11T12:00:00Z",
        source="fixture",
        url="https://example.com",
    )
    result = replay(intra, daily, cfg(tmp_path), "event", {"events": [event]})
    assert result["entries"] == 0


def test_replay_missing_calendar_and_risk_limit(tmp_path):
    intra, daily = fixture_frames(FIXTURES / "market.json")
    result = replay(intra, daily, cfg(tmp_path))
    assert result["entries"] == 0
    assert result["funnel"]["Upcoming earnings calendar unavailable"] > 0
    result = replay(intra, daily, cfg(tmp_path, max_order_notional=1, require_event_calendar=False))
    assert result["entries"] == 0


def test_time_exit_uses_same_session_limit(tmp_path):
    b, d = frames(64)
    extra = b.iloc[-1:].copy()
    extra.index = pd.DatetimeIndex([pd.Timestamp("2026-12-04T10:00:00", tz="America/New_York")])
    b = pd.concat([b, extra])
    result = replay(
        {"AAPL": b},
        {"AAPL": d, "SPY": d, "QQQ": d, "IWM": d},
        cfg(tmp_path, require_event_calendar=False),
    )
    assert result["closed_trades"] == 1 and result["exits"]["time"] == 1
    assert result["max_hold_sessions"] == 15


def test_no_next_available_bar_means_no_entry(tmp_path):
    b, d = frames(62)
    result = replay({"AAPL": b}, {"AAPL": d, "SPY": d}, cfg(tmp_path, require_event_calendar=False))
    assert result["entries"] == 0


def test_same_bar_ambiguity_and_gap_stop(tmp_path):
    b, d = frames(64)
    b.iloc[-1, b.columns.get_loc("high")] = 150
    b.iloc[-1, b.columns.get_loc("low")] = 110
    b.iloc[-1, b.columns.get_loc("open")] = 120
    result = replay(
        {"AAPL": b},
        {"AAPL": d, "SPY": d, "QQQ": d, "IWM": d},
        cfg(tmp_path, require_event_calendar=False),
    )
    assert result["exits"]["stop_both_touched"] == 1 and result["realized_pnl"] < 0


def test_comparison_uses_same_cost_and_risk_config(tmp_path):
    intra, daily = fixture_frames(FIXTURES / "market.json")
    result = compare(intra, daily, cfg(tmp_path, require_event_calendar=False))
    assert result["event"]["status"] == "UNAVAILABLE"
    assert result["ai"]["status"] == "UNAVAILABLE"
    assert set(result["cost_sensitivity"]) == {10, 20, 30}


def test_forward_rejected_markout_waits_for_observed_session_and_deduplicates(tmp_path):
    from trading_bot.observation import capture_markouts
    from trading_bot.state import Store

    from .helpers import candidate

    settings = cfg(tmp_path)
    c = candidate(settings)
    store = Store(settings.state_path)
    dates = pd.date_range("2026-11-11", periods=20, freq="B", tz="America/New_York")
    daily = pd.DataFrame({"close": [140.0] * 20}, index=dates)
    with store.writer():
        store.audit("candidate_rejected", {"candidate": c.model_dump(mode="json")}, NOW)
        capture_markouts(store, {"AAPL": daily}, NOW)
        assert not store.events("forward_markout")
        later = pd.Timestamp("2026-11-19T15:00:00Z").to_pydatetime()
        capture_markouts(store, {"AAPL": daily}, later)
        capture_markouts(store, {"AAPL": daily}, later)
        outcomes = store.events("forward_markout")
        assert len(outcomes) == 1
        assert outcomes[0]["payload"]["horizon_sessions"] == 5
        assert outcomes[0]["payload"]["gross_price_return"] > 0
        assert "NOT A TRADE" in outcomes[0]["payload"]["label"]
