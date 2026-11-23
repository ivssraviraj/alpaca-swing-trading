from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import requests

from trading_bot.execution import Broker
from trading_bot.reporting import SHEET_HEADER, SHEET_TITLE, Reporter

from .helpers import NOW, environment


def filled_env(tmp_path):
    e = environment(tmp_path)
    with e.store.writer():
        e.pipeline.run("paper-execute")
    return e


def test_missing_protection_recovery_with_kill_switch_and_ai_off(tmp_path):
    e = filled_env(tmp_path)
    e.trading.roots[0].legs[0].status = "canceled"
    settings = e.cfg.model_copy(update={"kill_switch": True, "ai_enabled": False})
    broker = Broker(e.trading, settings, e.store, "paper-execute", e.reporter.notify)
    with e.store.writer():
        snapshot = broker.manage(NOW)
    assert snapshot.safe
    assert len(e.trading.submissions) == 2
    assert e.trading.submissions[-1].order_class.value == "oco"


def test_time_exit_cancel_then_close_owned_quantity(tmp_path):
    e = filled_env(tmp_path)
    e.trading.roots[0].filled_at = NOW - timedelta(days=30)
    with e.store.writer():
        e.broker.manage(NOW)
    assert e.trading.cancellations
    assert e.trading.submissions[-1].side.value == "sell"
    assert e.trading.submissions[-1].qty == e.trading.submissions[0].qty
    assert not e.trading.current_positions


def test_cancel_pending_never_submits_exit(tmp_path):
    e = filled_env(tmp_path)
    e.trading.roots[0].filled_at = NOW - timedelta(days=30)
    e.trading.pending_cancel = True
    with e.store.writer():
        e.broker.manage(NOW)
    assert len(e.trading.submissions) == 1


def test_bracket_fills_during_cancel_no_double_sell(tmp_path):
    e = filled_env(tmp_path)
    e.trading.roots[0].filled_at = NOW - timedelta(days=30)

    def cancel(identifier):
        e.trading.exit("win")

    e.trading.cancel_order_by_id = cancel
    with e.store.writer():
        e.broker.manage(NOW)
    assert len(e.trading.submissions) == 1
    assert len(e.store.events("fill")) == 2


def test_manual_addition_to_owned_symbol_not_modified(tmp_path):
    e = filled_env(tmp_path)
    e.trading.current_positions[0].qty = str(float(e.trading.current_positions[0].qty) + 1)
    e.trading.roots[0].filled_at = NOW - timedelta(days=30)
    with e.store.writer():
        assert not e.broker.manage(NOW).safe
    assert len(e.trading.submissions) == 1 and not e.trading.cancellations


def test_notifications_only_fills_weekly_safety_and_dedup(tmp_path):
    e = filled_env(tmp_path)
    http = Mock()
    http.post.return_value = NS(raise_for_status=lambda: None)
    settings = e.cfg.model_copy(
        update={"alert_webhook_url": __import__("pydantic").SecretStr("https://example.com/hook")}
    )
    reporter = Reporter(settings, e.store, http)
    with e.store.writer():
        assert not reporter.notify("submission", "no", "bad")
        reporter.fills()
        reporter.fills()
        assert http.post.call_count == 1
        assert "PAPER TRADE BUY" in str(http.post.call_args)


def test_sheet_schema_failure_no_writes_and_sync_dedup(tmp_path):
    e = filled_env(tmp_path)
    reporter = Reporter(e.cfg.model_copy(update={"sheets_enabled": True}), e.store)
    book, tab = Mock(), Mock()
    book.worksheet.return_value = tab
    tab.row_values.return_value = ["old schema"]
    with e.store.writer():
        assert reporter.sheet(book) == "FAILED"
        tab.append_rows.assert_not_called()
        tab.row_values.return_value = SHEET_HEADER
        tab.col_values.return_value = ["event_key"]
        assert reporter.sheet(book) == "SUCCESS"
        row = tab.append_rows.call_args.args[0][0]
        assert row[1] == "ALPACA_PAPER"
        tab.col_values.return_value = ["event_key", row[0]]
        assert reporter.sheet(book) == "SUCCESS"
        assert tab.append_rows.call_count == 1
    assert all(c.args == (SHEET_TITLE,) for c in book.worksheet.call_args_list)


def test_failed_notification_not_marked_sent(tmp_path):
    e = filled_env(tmp_path)
    settings = e.cfg.model_copy(
        update={"alert_webhook_url": __import__("pydantic").SecretStr("https://example.com/hook")}
    )
    http = Mock()
    http.post.side_effect = requests.Timeout()
    reporter = Reporter(settings, e.store, http)
    with e.store.writer():
        assert not reporter.notify("safety", "test", "id")
        assert not e.store.get("notified:id")
        assert e.store.events("notification_failure")
