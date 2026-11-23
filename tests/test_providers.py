from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import NewsSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from trading_bot.evidence import EvidenceService
from trading_bot.providers import BoundedSession, MarketData, read_call
from trading_bot.state import Store
from trading_bot.universe import Universe

from .helpers import NOW, cfg, frames


def test_sdk_market_data_paginates_both_symbols(monkeypatch):
    client = StockHistoricalDataClient("fixture", "fixture")
    calls = []
    bar = {
        "t": "2026-11-09T15:00:00Z",
        "o": 10,
        "h": 12,
        "l": 9,
        "c": 11,
        "v": 100,
        "n": 10,
        "vw": 11,
    }

    def page(path, data):
        calls.append(dict(data))
        if data.get("page_token") is None:
            return {"bars": {"AAPL": [bar]}, "next_page_token": "next"}
        return {"bars": {"MSFT": [bar]}, "next_page_token": None}

    monkeypatch.setattr(client, "get", page)
    result = client.get_stock_bars(
        StockBarsRequest(symbol_or_symbols=["AAPL", "MSFT"], timeframe=TimeFrame.Day)
    )
    assert set(result.data) == {"AAPL", "MSFT"} and len(calls) == 2
    assert calls[1]["page_token"] == "next"


def test_sdk_news_response_contract(tmp_path):
    settings = cfg(tmp_path)
    raw = {
        "news": [
            {
                "id": 1,
                "headline": "Company reports earnings",
                "source": "fixture",
                "url": "https://example.com/test",
                "symbols": ["AAPL"],
                "summary": "Synthetic fixture content",
                "created_at": "2026-11-10T18:00:00Z",
                "updated_at": "2026-11-10T18:00:00Z",
                "author": "fixture",
                "content": "Synthetic",
                "images": [],
            }
        ],
        "next_page_token": None,
    }
    client = Mock(spec=NewsClient)
    client.get_news.return_value = NewsSet(raw)
    store = Store(settings.state_path)
    with store.writer():
        result = EvidenceService(client, settings, store).news("AAPL", NOW)
    assert len(result) == 1 and result[0].source == "fixture"


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError()])
def test_transient_read_retry(monkeypatch, failure):
    monkeypatch.setattr("trading_bot.providers.time.sleep", lambda _: None)
    fn = Mock(side_effect=[failure, "ok"])
    assert read_call(fn) == "ok" and fn.call_count == 2


def test_rate_limit_retry_and_permanent_no_retry(monkeypatch):
    monkeypatch.setattr("trading_bot.providers.time.sleep", lambda _: None)
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "1"
    fn = Mock(side_effect=[requests.HTTPError(response=response), "ok"])
    assert read_call(fn) == "ok"
    response.status_code = 403
    fn = Mock(side_effect=requests.HTTPError(response=response))
    with pytest.raises(requests.HTTPError):
        read_call(fn)
    assert fn.call_count == 1


def test_transport_timeout_and_redirects(monkeypatch):
    call = Mock(return_value="ok")
    monkeypatch.setattr(requests.Session, "request", call)
    session = BoundedSession(5, 0)
    session.get("https://example.com")
    assert call.call_args.kwargs["timeout"] == 5
    assert not call.call_args.kwargs["allow_redirects"]


def test_data_failure_explicit_no_frames(tmp_path):
    settings = cfg(tmp_path)
    store = Store(settings.state_path)
    client = Mock()
    client.get_stock_bars.side_effect = ValueError("unavailable")
    with store.writer():
        result = MarketData(client, settings, store).bars(["AAPL"], NOW - timedelta(days=5), NOW)
        assert not result
        assert store.events("data_failure")
        assert store.events("coverage")[-1]["payload"]["missing"] == ["AAPL"]


def test_universe_daily_reuse_liquidity_and_snapshot(tmp_path):
    settings = cfg(tmp_path, universe_size=100)
    store = Store(settings.state_path)
    broker = Mock()
    broker.get_all_assets.return_value = [
        NS(
            symbol=s,
            name=s,
            status="active",
            asset_class="us_equity",
            tradable=True,
            exchange="NASDAQ",
        )
        for s in ["AAPL", "LOW"]
    ]
    _, frame = frames()
    low = frame.copy()
    low.volume = 1
    data = Mock()
    data.daily.return_value = {"AAPL": frame, "LOW": low}
    service = Universe(broker, data, settings, store)
    with store.writer():
        selected = service.build(NOW)
        assert [s["symbol"] for s in selected] == ["AAPL"]
        assert service.build(NOW) == selected
        assert broker.get_all_assets.call_count == 1
        assert (settings.reports_dir / "universe.json").exists()
