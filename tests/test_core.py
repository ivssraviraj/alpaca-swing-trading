from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pandas as pd
import pytest
from pydantic import ValidationError

from trading_bot.ai import AIService, evidence_hash, validate_approval
from trading_bot.calendar import coverage, entry_session, held_sessions, regular_completed
from trading_bot.config import Settings
from trading_bot.evidence import imminent_earnings, validate_news
from trading_bot.models import Approval, Event, fingerprint
from trading_bot.risk import plan_order
from trading_bot.signals import regime, scan, setup_flags
from trading_bot.state import Store
from trading_bot.universe import asset_eligible

from .helpers import NOW, FakeAI, candidate, cfg, decision, environment, frames, news


@pytest.mark.parametrize(
    "values",
    [
        dict(apca_api_base_url="https://api.alpaca.markets"),
        dict(max_hold_sessions=16),
        dict(max_hold_sessions=21, extended_hold_ack=True),
        dict(risk_per_trade=0.05),
        dict(max_total_risk=0.05),
        dict(slippage_bps=0),
        dict(min_stop_fraction=0.1, max_stop_fraction=0.02),
        dict(strategy_capital_usd=-1),
    ],
)
def test_invalid_config(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_defaults_and_secret_redaction():
    s = Settings(_env_file=None, openai_api_key="secret-fixture")
    assert not s.paper_orders_enabled and not s.ai_enabled and s.max_hold_sessions == 15
    assert "secret-fixture" not in repr(s)
    with pytest.raises(ValueError):
        s.execution_gate("paper-execute")


@pytest.mark.parametrize(
    "when,expected",
    [
        ("2026-11-26T16:00:00+00:00", False),  # Thanksgiving
        ("2026-11-27T17:00:00+00:00", True),  # early close 18:00 UTC
        ("2026-11-27T17:40:00+00:00", False),
        ("2026-07-06T14:00:00+00:00", True),  # EDT
        ("2026-11-10T15:00:00+00:00", True),  # EST
        ("2026-11-22T16:00:00+00:00", False),
    ],
)
def test_calendar(when, expected):
    assert entry_session(datetime.fromisoformat(when)) == expected


def test_holding_sessions_ignores_weekend_holiday():
    assert (
        held_sessions(pd.Timestamp("2026-11-25T16:00:00Z"), pd.Timestamp("2026-11-30T16:00:00Z"))
        == 2
    )


def test_completed_bar_early_close_and_timezone():
    index = pd.date_range("2026-11-27 12:50", periods=4, freq="5min", tz="America/New_York")
    f = pd.DataFrame({"close": [1, 2, 3, 4]}, index=index)
    output = regular_completed(f, datetime(2026, 11, 27, 19, tzinfo=timezone.utc))
    assert len(output) == 2 and str(output.index.tz) == "UTC"
    assert len(regular_completed(f, datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc))) == 1
    assert coverage(output)["fraction"] == 1
    with pytest.raises(ValueError):
        regular_completed(f.tz_localize(None), NOW)


def test_signal_warmup_and_no_future_dependency(tmp_path):
    settings = cfg(tmp_path)
    c = candidate(settings)
    b, d = frames()
    r = c.regime
    assert not scan("AAPL", "Fixture Company", b.iloc[:60], d, d, [], r, settings, NOW)
    assert not scan("AAPL", "Fixture Company", b, d.iloc[:10], d, [], r, settings, NOW)
    future = d.copy()
    future.loc[pd.Timestamp("2026-11-11T00:00:00", tz="America/New_York")] = [1, 1, 1, 1, 100]
    found = scan("AAPL", "Fixture Company", b, future, d, [], r, settings, NOW)
    assert found[0] == c


def test_nine_setups_and_ordered_retest():
    b, d = frames()
    result = setup_flags(b, d, 0.05, True)
    assert len(result) == 9
    assert (
        result["breakout"]
        and result["relative_strength_breakout"]
        and result["post_earnings_momentum"]
    )
    assert not result["breakout_retest"]  # no prior breakout followed by retest
    b.iloc[-5, b.columns.get_loc("close")] = 128
    b.iloc[-2, b.columns.get_loc("low")] = 127
    b.iloc[-2, b.columns.get_loc("close")] = 127.1
    assert setup_flags(b, d, 0.05, True)["breakout_retest"]


@pytest.mark.parametrize("direction,expected", [(1, "SUPPORTIVE"), (-1, "HOSTILE")])
def test_regime_direction(direction, expected):
    _, d = frames()
    if direction < 0:
        d["close"] = d.close.iloc[::-1].to_numpy()
    result = regime({s: d for s in ["SPY", "QQQ", "IWM"]}, NOW)
    assert result.classification == expected
    assert result.size_multiplier == (0.5 if direction < 0 else 1)
    assert regime({}, NOW).classification == "NEUTRAL"


@pytest.mark.parametrize(
    "changes,eligible",
    [
        ({}, True),
        ({"tradable": False}, False),
        ({"exchange": "OTC"}, False),
        ({"name": "Daily 3x Bull ETF"}, False),
        ({"name": "Inverse Market Fund"}, False),
        ({"asset_class": "crypto"}, False),
    ],
)
def test_universe_metadata(changes, eligible):
    props = dict(
        status="active",
        tradable=True,
        asset_class="us_equity",
        exchange="NASDAQ",
        name="Company Inc",
        symbol="AAPL",
    )
    props.update(changes)
    assert asset_eligible(NS(**props)) == eligible


def test_news_chronology_and_historical_archive():
    n = news()
    validate_news(n, "AAPL", NOW, 120)
    with pytest.raises(ValueError, match="archived"):
        validate_news(n, "AAPL", NOW - timedelta(minutes=30), 120, historical=True)
    with pytest.raises(ValueError):
        validate_news(n, "MSFT", NOW, 120)
    with pytest.raises(ValueError):
        validate_news(n, "AAPL", NOW + timedelta(days=10), 120)


def test_earnings_blackout_session_based():
    event = Event(
        id="e",
        symbol="AAPL",
        kind="earnings_calendar",
        event_at=datetime(2026, 11, 30, tzinfo=timezone.utc),
        known_at=NOW,
        source="fixture",
        url="https://example.com",
    )
    assert imminent_earnings("AAPL", [event], datetime(2026, 11, 25, 16, tzinfo=timezone.utc), 2)


def test_ai_cache_budget_and_evidence_binding(tmp_path):
    settings = cfg(tmp_path, ai_max_calls_daily=1)
    store = Store(settings.state_path)
    client = FakeAI()
    service = AIService(settings, store, client)
    c, n = candidate(settings), news()
    with store.writer():
        a = service.evaluate(c, n, [], NOW)
        validate_approval(a, c, n, NOW, settings)
        assert service.evaluate(c, n, [], NOW) == a
        assert client.calls == 1
        changed = [n[0].model_copy(update={"summary": "changed evidence"})]
        with pytest.raises(ValueError, match="budget"):
            service.evaluate(c, changed, [], NOW)
        forged = a.model_copy(
            update={
                "decision": a.decision.model_copy(
                    update={
                        "evidence": [
                            a.decision.evidence[0].model_copy(
                                update={"url": "https://example.com/unknown"}
                            )
                        ]
                    }
                )
            }
        )
        with pytest.raises(ValueError, match="citation"):
            validate_approval(forged, c, n, NOW, settings)


def test_single_writer_and_persistent_intents(tmp_path):
    first = Store(tmp_path / "state.db")
    second = Store(tmp_path / "state.db")
    with first.writer():
        with pytest.raises(RuntimeError, match="writer lock"):
            with second.writer():
                pass
        assert first.intent("same-signal", "AAPL", {})
        assert not first.intent("same-signal", "AAPL", {})
    with second.writer():
        assert not second.intent("same-signal", "AAPL", {})


@pytest.mark.parametrize(
    "mutation", ["sector", "correlation", "total_risk", "exposure", "pending", "calendar"]
)
def test_portfolio_risk_gates(tmp_path, mutation):
    e = environment(tmp_path)
    c, n = candidate(e.cfg), news()
    a = Approval(
        candidate_hash=fingerprint(c.model_dump(mode="json")),
        evidence_hash=evidence_hash(n),
        decision=decision(c, n),
        evaluated_at=NOW,
    )
    with e.store.writer():
        p = e.broker.reconcile(NOW)
        correlations = {}
        if mutation == "sector":
            p = p.model_copy(update={"sector_counts": {"UNKNOWN": 2}})
        if mutation == "correlation":
            p = p.model_copy(update={"positions": {"MSFT": 1, "NVDA": 1}})
            correlations = {"MSFT": 0.99, "NVDA": 0.99}
        if mutation == "total_risk":
            p = p.model_copy(update={"stop_risk": 1000})
        if mutation == "exposure":
            p = p.model_copy(update={"exposure": 16000})
        if mutation == "pending":
            p = p.model_copy(update={"pending_symbols": ["AAPL"]})
        with pytest.raises(ValueError):
            plan_order(
                c,
                a,
                n,
                e.market.quote("AAPL"),
                p,
                e.cfg,
                NOW,
                calendar_ok=mutation != "calendar",
                correlations=correlations,
            )


def test_changed_fundamental_context_invalidates_cached_approval(tmp_path):
    settings = cfg(tmp_path)
    store = Store(settings.state_path)
    c, items = candidate(settings), news()
    event = Event(
        id="earnings",
        symbol="AAPL",
        kind="earnings_calendar",
        event_at=NOW - timedelta(days=1),
        known_at=NOW,
        source="fixture",
        url="https://example.com",
        facts={"epsActual": 1.2, "epsEstimate": 1.1},
    )
    with store.writer():
        approval = AIService(settings, store, FakeAI()).evaluate(c, items, [event], NOW)
        validate_approval(approval, c, items, NOW, settings, events=[event])
        changed = event.model_copy(update={"facts": {"epsActual": 0.8, "epsEstimate": 1.1}})
        with pytest.raises(ValueError, match="context changed"):
            validate_approval(approval, c, items, NOW, settings, events=[changed])


def test_direct_submission_without_approval_intent_is_blocked(tmp_path):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    e = environment(tmp_path)
    request = MarketOrderRequest(
        symbol="AAPL",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id="unapproved",
    )
    with e.store.writer(), pytest.raises(ValueError, match="intent"):
        e.broker.submit_once(request, NOW)
    assert not e.trading.submissions


def test_overlapping_process_is_blocked_by_same_durable_lock(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "shared.db"
    store = Store(path)
    child = """import sys
from pathlib import Path
from trading_bot.state import Store
store = Store(Path(sys.argv[1]))
try:
    with store.writer():
        raise SystemExit(0)
except RuntimeError:
    raise SystemExit(42)
"""
    with store.writer():
        result = subprocess.run(
            [sys.executable, "-c", child, str(path)], timeout=10, capture_output=True
        )
    assert result.returncode == 42
