from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from trading_bot.evidence import EvidenceService
from trading_bot.reporting import Reporter
from trading_bot.state import Store

from .helpers import NOW, cfg, environment


def test_finnhub_calendar_actual_consensus_and_hourly_snapshot(tmp_path):
    settings = cfg(tmp_path, finnhub_api_key="fixture")
    store = Store(settings.state_path)
    service = EvidenceService(NS(), settings, store)
    response = Mock()
    response.json.return_value = {
        "earningsCalendar": [
            {
                "symbol": "AAPL",
                "date": "2026-11-11",
                "epsActual": None,
                "epsEstimate": 1.5,
                "revenueActual": None,
                "revenueEstimate": 100,
                "hour": "bmo",
            }
        ]
    }
    service.http = Mock()
    service.http.get.return_value = response
    with store.writer():
        events, available = service.calendar(NOW)
        assert available and events[0].facts["epsActual"] is None
        assert events[0].facts["epsEstimate"] == 1.5
        assert service.calendar(NOW)[0] == events
        assert service.http.get.call_count == 1
        store.export(settings.reports_dir)
        assert store.events("calendar_snapshot")
        service.calendar(NOW + timedelta(hours=1))
        assert service.http.get.call_count == 2


def test_macro_provider_rejects_ambiguous_timestamp(tmp_path):
    settings = cfg(tmp_path, macro_calendar_enabled=True, finnhub_api_key="fixture")
    store = Store(settings.state_path)
    service = EvidenceService(NS(), settings, store)
    service.http = Mock()
    service.http.get.return_value.json.return_value = {
        "economicCalendar": [
            {
                "country": "US",
                "time": "2026-11-10 20:00:00",
                "event": "Synthetic macro",
                "impact": "high",
            }
        ]
    }
    with store.writer():
        with pytest.raises(ValueError, match="timezone"):
            service.macro(NOW)
        service.http.get.return_value.json.return_value["economicCalendar"][0]["time"] = (
            "2026-11-10T20:00:00Z"
        )
        events, available = service.macro(NOW)
        assert available and events[0].facts["impact"] == "high"


def test_weekly_report_only_saturday_and_closed_trade_metrics(tmp_path):
    e = environment(tmp_path)
    with e.store.writer():
        e.pipeline.run("paper-execute")
        e.trading.exit("win")
        e.broker.reconcile(NOW + timedelta(minutes=5))
        http = Mock()
        settings = e.cfg.model_copy(
            update={"alert_webhook_url": SecretStr("https://example.com/hook")}
        )
        reporter = Reporter(settings, e.store, http)
        report = reporter.weekly(NOW + timedelta(hours=1))
        assert report["closed_trade_win_rate"] == 1
        assert len(report["closed_positions"]) == 1
        assert not http.post.called
        saturday = NOW + timedelta(days=4)
        reporter.weekly(saturday)
        reporter.weekly(saturday)
        assert http.post.call_count == 1
