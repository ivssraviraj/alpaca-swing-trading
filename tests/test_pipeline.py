from datetime import timedelta

import pytest

from trading_bot.execution import Broker
from trading_bot.models import Quote

from .helpers import NOW, environment


@pytest.mark.parametrize("outcome,sign", [("win", 1), ("loss", -1)])
def test_end_to_end_market_ai_risk_fill_exit_ledger(tmp_path, outcome, sign):
    from unittest.mock import Mock

    e = environment(tmp_path, alert_webhook_url="https://example.com/mock-only")
    e.reporter.http = Mock()
    with e.store.writer():
        result = e.pipeline.run("paper-execute")
        assert result["submitted"] == 1
        assert len(e.trading.submissions) == 1
        assert len(e.store.events("fill")) == 1
        assert e.broker.reconcile(NOW).safe
        e.trading.exit(outcome)
        assert e.broker.reconcile(NOW + timedelta(minutes=5)).safe
        fills = e.store.events("fill")
        assert len(fills) == 2
        assert fills[-1]["payload"]["realized_pnl"] * sign > 0
        e.broker.reconcile(NOW + timedelta(minutes=5))
        assert len(e.store.events("fill")) == 2
        e.reporter.fills()
        assert e.reporter.http.post.call_count == 2


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "reject",
        "uncertain",
        "missing_news",
        "expired",
        "wrong_symbol",
        "missing_evidence",
        "critical_risks",
        "future_news",
        "changed_news",
        "changed_signal",
        "stale_bar",
        "missing_data",
        "stale_quote",
        "chase",
        "risk",
        "closed",
        "kill",
        "budget",
        "bad_schema",
    ],
)
def test_fail_closed_paths_never_submit(tmp_path, failure):
    overrides = (
        {"kill_switch": True}
        if failure == "kill"
        else {"ai_max_calls_daily": 0}
        if failure == "budget"
        else {}
    )
    e = environment(tmp_path, **overrides)
    if failure == "timeout":
        e.ai_client.fail = TimeoutError()
    if failure in {"reject", "uncertain"}:
        e.ai_client.verdict = failure.upper()
    if failure == "missing_news":
        e.evidence.items = []
    if failure == "expired":
        e.ai_client.mutate = lambda d: d.model_copy(
            update={"approval_expires_at": NOW - timedelta(seconds=1)}
        )
    if failure == "wrong_symbol":
        e.ai_client.mutate = lambda d: d.model_copy(update={"symbol": "MSFT"})
    if failure == "missing_evidence":
        e.ai_client.mutate = lambda d: d.model_copy(update={"evidence": []})
    if failure == "critical_risks":
        e.ai_client.mutate = lambda d: d.model_copy(update={"critical_risks": ["Unresolved"]})
    if failure == "bad_schema":
        e.ai_client.mutate = lambda d: {"decision": "yes"}
    if failure == "future_news":
        e.evidence.items = [
            e.evidence.items[0].model_copy(update={"published_at": NOW + timedelta(hours=1)})
        ]
    if failure == "changed_news":
        e.evidence.changed = True
    if failure == "changed_signal":
        e.market.changed = True
    if failure == "stale_bar":
        e.market.bars.index -= timedelta(hours=1)
    if failure == "missing_data":
        e.market.missing = True
    if failure == "stale_quote":
        e.market.latest_quote = e.market.latest_quote.model_copy(
            update={"at": NOW - timedelta(minutes=5)}
        )
    if failure == "chase":
        e.market.latest_quote = Quote(symbol="AAPL", bid=140, ask=140.01, at=NOW, feed="iex")
    if failure == "risk":
        e.trading.account.cash = "0"
    if failure == "closed":
        e.trading.open = False
    with e.store.writer():
        e.pipeline.run("paper-execute")
    assert not e.trading.submissions, e.store.events("candidate_rejected")


@pytest.mark.parametrize("mode", ["observe", "paper-dry-run", "replay"])
def test_read_only_modes_cannot_submit(tmp_path, mode):
    e = environment(tmp_path, mode=mode)
    with e.store.writer():
        e.pipeline.run(mode)
        with pytest.raises(ValueError, match="disabled"):
            e.broker.mutation_gate()
    assert not e.trading.submissions


def test_duplicate_same_run_and_restart(tmp_path):
    e = environment(tmp_path)
    with e.store.writer():
        e.pipeline.run("paper-execute")
        e.pipeline.run("paper-execute")
    assert len(e.trading.submissions) == 1
    from trading_bot.state import Store

    restarted = Store(e.cfg.state_path)
    broker = Broker(e.trading, e.cfg, restarted, "paper-execute", e.reporter.notify)
    with restarted.writer():
        assert broker.reconcile(NOW).safe
        assert len(restarted.events("fill")) == 1
    assert len(e.trading.submissions) == 1


@pytest.mark.parametrize("accepted", [True, False])
def test_submission_timeout_reconciles_never_retries(tmp_path, accepted):
    e = environment(tmp_path)
    e.trading.timeout, e.trading.fail_submission = accepted, not accepted
    with e.store.writer():
        e.pipeline.run("paper-execute")
        e.pipeline.run("paper-execute")
    assert len(e.trading.submissions) == 1
    assert bool(e.store.events("fill")) == accepted


def test_partial_fill_recovers_actual_quantity(tmp_path):
    e = environment(tmp_path)
    e.trading.fill_ratio = 0.5
    with e.store.writer():
        e.pipeline.run("paper-execute")
        assert len(e.trading.submissions) == 2
        assert e.trading.submissions[1].qty == e.trading.submissions[0].qty / 2
        assert e.trading.submissions[1].side.value == "sell"
        assert e.broker.reconcile(NOW).safe
        assert len(e.store.events("fill")) == 1


def test_unowned_manual_position_not_changed(tmp_path):
    from types import SimpleNamespace

    e = environment(tmp_path)
    e.trading.current_positions = [
        SimpleNamespace(
            symbol="AAPL",
            qty="15",
            side="long",
            current_price="131",
            avg_entry_price="120",
            unrealized_pl="165",
        )
    ]
    with e.store.writer():
        result = e.pipeline.run("paper-execute")
        assert result["status"] == "ENTRY_FREEZE"
    assert not e.trading.submissions and not e.trading.cancellations


def test_wrong_endpoint_rejected(tmp_path):
    e = environment(tmp_path)
    e.trading._base_url = "https://api.alpaca.markets"
    with pytest.raises(ValueError, match="endpoint"):
        Broker(e.trading, e.cfg, e.store, "paper-execute", e.reporter.notify)
