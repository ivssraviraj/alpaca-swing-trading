from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from trading_bot.signals import setup_flags
from trading_bot.state import Store

from .helpers import NOW, cfg, environment, frames, order


@pytest.mark.parametrize(
    "setup",
    ["pullback", "trend_continuation", "gap_and_go", "post_earnings_drift", "earnings_reversal"],
)
def test_each_additional_setup_has_objective_fixture(setup):
    bars, daily = frames()
    prices = [126 + i * 0.05 for i in range(61)]
    volume = 12000
    if setup == "pullback":
        prices[-2:] = [127.8, 129.2]
        volume = 10000
    elif setup == "gap_and_go":
        prices = [131.0] * 60 + [135.0]
        volume = 30000
    elif setup == "earnings_reversal":
        prices = [122.0] * 51 + [121, 120, 119, 118, 119, 119.5, 120, 120.5, 121, 123]
        volume = 30000
    else:
        prices[-1] += 0.2
    bars["open"] = bars["close"] = prices
    bars["high"] = bars.close + 0.1
    bars["low"] = bars.close - 0.1
    bars.loc[bars.index[-1], "volume"] = volume
    flags = setup_flags(bars, daily, 0.05, True)
    assert flags[setup], flags
    if setup in {"post_earnings_drift", "earnings_reversal"}:
        assert not setup_flags(bars, daily, 0.05, False)[setup]


def test_order_history_pagination_and_tie_failure(tmp_path):
    e = environment(tmp_path)
    page = [
        order(str(i), status="canceled", submitted_at=NOW - timedelta(seconds=i))
        for i in range(500)
    ]

    def get_orders(filter):
        if filter.status.value == "open":
            return []
        return (
            page
            if filter.until is None
            else [order("old", status="canceled", submitted_at=NOW - timedelta(days=1))]
        )

    e.trading.get_orders = get_orders
    with e.store.writer():
        assert len(e.broker.orders()) == 501
    e.trading.get_orders = lambda filter: [] if filter.status.value == "open" else page
    with e.store.writer():
        with pytest.raises(ValueError, match="pagination"):
            e.broker.orders()


def test_disabled_paper_and_scheduled_ephemeral_state_block(tmp_path):
    e = environment(tmp_path, paper_orders_enabled=False)
    with e.store.writer(), pytest.raises(ValueError, match="disabled"):
        e.broker.mutation_gate()
    e = environment(tmp_path, github_actions=True)
    with e.store.writer(), pytest.raises(ValueError, match="persistent"):
        e.broker.mutation_gate()


def test_postgres_writer_lock_contract(monkeypatch, tmp_path):
    from unittest.mock import Mock

    import psycopg

    db = Mock()
    db.execute.return_value.fetchone.return_value = (False,)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: db)
    store = Store(tmp_path / "unused", "postgresql://fixture")
    with pytest.raises(RuntimeError, match="writer lock"):
        with store.writer():
            pass
    db.execute.return_value.fetchone.return_value = (True,)
    with store.writer():
        store.require_lock()
    assert any("pg_advisory_unlock" in call.args[0] for call in db.execute.call_args_list)


def test_incremental_refresh_requests_overlap_not_full_history(tmp_path, monkeypatch):
    from trading_bot.providers import MarketData

    settings = cfg(tmp_path)
    store = Store(settings.state_path)
    data = MarketData(NS(), settings, store)
    bars, _ = frames()
    calls = []

    def fetch(symbols, start, end, daily=False):
        calls.append(start)
        return {"AAPL": bars.tz_convert("UTC")}

    monkeypatch.setattr(data, "bars", fetch)
    with store.writer():
        data.intraday(["AAPL"], NOW)
        data.intraday(["AAPL"], NOW + timedelta(minutes=5))
    assert calls[0] == NOW - timedelta(days=7)
    assert calls[1] == bars.index[-1] - timedelta(minutes=10)


def test_replay_cli_cannot_construct_broker(tmp_path, monkeypatch):
    import trading_bot.providers
    from trading_bot.cli import main

    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        trading_bot.providers, "clients", lambda _: pytest.fail("Replay used broker")
    )
    assert (
        main(
            [
                "replay",
                "--fixture",
                "tests/fixtures/market.json",
                "--archive",
                "tests/fixtures/events.json",
            ]
        )
        == 0
    )
