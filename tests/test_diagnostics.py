import json
import logging
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import requests

from scripts.write_run_summary import build_summary
from trading_bot import diagnostics as log
from trading_bot.cli import main
from trading_bot.providers import BoundedSession
from trading_bot.signals import regime, scan
from trading_bot.state import Store
from trading_bot.universe import Universe

from .helpers import NOW, cfg, environment, frames


@pytest.fixture
def logger_state():
    handlers, level, propagate = list(log.LOGGER.handlers), log.LOGGER.level, log.LOGGER.propagate
    token = log._context.set({})
    secrets = set(log._secrets)
    # configure() must not close pytest's or another caller's existing handlers.
    log.LOGGER.handlers = []
    yield
    for handler in list(log.LOGGER.handlers):
        handler.close()
    log.LOGGER.handlers = handlers
    log.LOGGER.setLevel(level)
    log.LOGGER.propagate = propagate
    log._context.reset(token)
    log._secrets.clear()
    log._secrets.update(secrets)


def read_runtime(directory):
    return [json.loads(line) for line in (directory / "runtime.jsonl").read_text().splitlines()]


def test_runtime_redacts_credentials_bodies_and_provider_errors(
    tmp_path, monkeypatch, logger_state
):
    settings = cfg(tmp_path, apca_api_key_id="private-test-key")
    run = log.configure(settings, "paper-dry-run")
    response = requests.Response()
    response.status_code = 403
    error = requests.HTTPError("PRIVATE_RESPONSE_BODY", response=response)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=error))
    with pytest.raises(requests.HTTPError):
        BoundedSession(1, 0).get(
            "https://user:private-pass@example.com/private-path?token=private-query"
        )
    log.emit(
        "redaction_probe",
        api_key="unregistered-secret",
        headers={"Authorization": "private-header"},
        response_body="private-body",
        explanation="private-test-key Bearer private-bearer postgres://user:private-db@db/name",
        value=float("nan"),
    )
    contents = (settings.reports_dir / "runtime.jsonl").read_text()
    for secret in (
        "private-test-key",
        "private-pass",
        "private-path",
        "private-query",
        "private-header",
        "private-body",
        "private-bearer",
        "private-db",
        "unregistered-secret",
        "PRIVATE_RESPONSE_BODY",
    ):
        assert secret not in contents
    rows = read_runtime(settings.reports_dir)
    assert all(r["run_id"] == run and r["mode"] == "paper-dry-run" for r in rows)
    failed = next(r for r in rows if r["event"] == "http_failure")
    assert failed["http_status"] == 403 and failed["error_type"] == "HTTPError"
    assert failed["host"] == "example.com"
    assert rows[-1]["value"] is None


def test_logger_failure_does_not_mask_operation_or_exception(monkeypatch, capsys, logger_state):
    log.LOGGER.setLevel(logging.DEBUG)
    monkeypatch.setattr(log.LOGGER, "log", Mock(side_effect=OSError("private filesystem detail")))

    @log.traced("fixture.operation")
    def operation(fail=False):
        if fail:
            raise ValueError("original failure")
        return "accepted"

    assert operation() == "accepted"
    with pytest.raises(ValueError, match="original failure"):
        operation(True)
    output = capsys.readouterr().err
    assert "diagnostic_logging_failed" in output
    assert "private filesystem detail" not in output


@pytest.mark.parametrize(
    "case,reason",
    [
        ("bars", "insufficient_bars"),
        ("coverage", "insufficient_intraday_coverage"),
        ("stale", "stale_or_future_signal"),
        ("gap", "recent_bar_gaps"),
        ("zero_price", "price_below_minimum"),
        ("stop", "stop_distance_exceeds_limit"),
        ("reward", "net_reward_risk_below_minimum"),
        ("pattern", "no_matching_setup"),
    ],
)
def test_scanner_explains_rejections_without_changing_results(tmp_path, case, reason):
    settings = cfg(tmp_path)
    bars, daily = frames()
    now = NOW
    if case == "bars":
        daily = daily.iloc[:10]
    elif case == "coverage":
        settings = settings.model_copy(update={"min_intraday_coverage": 1.01})
    elif case == "stale":
        now += timedelta(minutes=31)
    elif case == "gap":
        bars, daily = frames(62)
        bars = bars.drop(bars.index[-3])
        now += timedelta(minutes=5)
    elif case == "zero_price":
        bars.iloc[-1, bars.columns.get_loc("close")] = 0
    elif case == "stop":
        settings = settings.model_copy(update={"max_stop_fraction": 0.01})
    elif case == "reward":
        settings = settings.model_copy(update={"min_net_reward_risk": 10})
    elif case == "pattern":
        bars.iloc[-1] = bars.iloc[-2]
    args = ("AAPL", "Fixture", bars, daily, daily, [], regime({}, now), settings, now)
    detail = {}
    assert scan(*args, diagnostics=detail) == scan(*args) == []
    assert detail["first_failure"] == reason
    assert detail["outcome"] == "rejected" and detail["symbol"] == "AAPL"
    if case == "stop":
        assert detail["patterns"]["breakout"]
        assert detail["stop_fraction"] > detail["max_stop_fraction"]
    if case == "pattern":
        assert not any(detail["patterns"].values())
        assert not detail["pattern_inputs"]["checks"]["breakout_price"]


def test_scanner_accepted_candidates_identical_with_diagnostics(tmp_path):
    bars, daily = frames()
    args = ("AAPL", "Fixture", bars, daily, daily, [], regime({}, NOW), cfg(tmp_path), NOW)
    detail = {}
    result = scan(*args, diagnostics=detail)
    assert result and result == scan(*args)
    assert detail["first_failure"] is None and detail["signal_count"] == len(result)
    assert detail["setups"] == [c.setup for c in result]


def test_zero_signal_run_explains_overlapping_failures_without_ai_or_orders(tmp_path, logger_state):
    e = environment(tmp_path, mode="paper-dry-run", min_stop_fraction=0.005, max_stop_fraction=0.01)
    run = log.configure(e.cfg, "paper-dry-run")
    with e.store.writer():
        result = e.pipeline.run("paper-dry-run")
    assert result["signals"] == 0 and result["rejection_counts"] == {
        "stop_distance_exceeds_limit": 1
    }
    assert result["ai_review_attempts"] == result["risk_approved"] == 0
    assert result["pattern_evaluated"] == result["pattern_matched_symbols"] == 1
    assert result["setup_pass_counts"]["breakout"] == 1
    assert e.ai_client.calls == 0 and not e.trading.submissions
    detail = e.store.events("scan_diagnostic")[0]["payload"]
    assert detail["run_id"] == result["run_id"] == run
    assert detail["phase"] == "initial" and detail["as_of"] == NOW.isoformat()
    exported = json.loads((e.cfg.reports_dir / "scan_diagnostics.json").read_text())
    assert exported["symbols"][0]["first_failure"] == "stop_distance_exceeds_limit"
    log.emit("run_finished", "INFO", exit_code=0, elapsed_seconds=1)
    summary = build_summary(e.cfg.reports_dir)
    assert "| signals | 0 |" in summary and "stop_distance_exceeds_limit | 1" in summary
    assert "Sector mapping is incomplete" in summary
    e.store.close()


def test_shortlist_failure_identifies_ai_stage(tmp_path):
    e = environment(tmp_path, mode="paper-dry-run", ai_enabled=False)
    with e.store.writer():
        result = e.pipeline.run("paper-dry-run")
    assert result["signals"] > 0 and result["shortlisted"] == 1
    assert result["candidate_rejection_counts"] == {"ai_review": 1}
    assert result["ai_review_attempts"] == 1 and result["risk_approved"] == 0
    failure = e.store.events("candidate_rejected")[-1]["payload"]
    assert failure["stage"] == "ai_review" and "AI disabled" in failure["reason"]
    assert not e.trading.submissions
    e.store.close()


def test_disable_debug_keeps_business_audit_and_scan_summary(tmp_path, logger_state):
    e = environment(tmp_path, mode="paper-dry-run", debug_traces_enabled=False, log_level="INFO")
    e.store.debug_traces_enabled = False
    log.configure(e.cfg, "paper-dry-run")
    with e.store.writer():
        result = e.pipeline.run("paper-dry-run")
    assert result["signals"] and result["conditional_setups"] == 1
    assert not e.store.events("scan_diagnostic")
    assert e.store.events("scan") and e.store.events("risk_approved")
    assert (e.cfg.reports_dir / "scan_summary.json").exists()
    assert not (e.cfg.reports_dir / "scan_diagnostics.json").exists()
    assert all(r["level"] != "DEBUG" for r in read_runtime(e.cfg.reports_dir))
    assert not e.trading.submissions
    e.store.close()


def test_audit_payload_cannot_override_runtime_metadata(tmp_path, logger_state):
    settings = cfg(tmp_path)
    run = log.configure(settings, "observe")
    store = Store(settings.state_path)
    payload = {"event": "run_started", "level": "ERROR", "as_of": "observation", "run_id": "other"}
    with store.writer():
        store.trace("scan_diagnostic", payload, NOW)
    row = next(
        r for r in read_runtime(settings.reports_dir) if r["event"] == "audit.scan_diagnostic"
    )
    assert row["run_id"] == run and row["level"] == "INFO"
    assert store.events("scan_diagnostic")[0]["payload"]["run_id"] == run
    assert row["details"]["as_of"] == "observation"
    store.close()


def test_summary_ignores_prior_run_scan_and_failure(tmp_path):
    rows = [
        {"event": "run_started", "run_id": "old", "mode": "observe"},
        {"event": "workflow_failed", "run_id": "old", "level": "ERROR", "reason": "old-failure"},
        {"event": "run_started", "run_id": "new", "mode": "check-ai"},
        {"event": "run_finished", "run_id": "new", "exit_code": 0, "elapsed_seconds": 2},
    ]
    (tmp_path / "runtime.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\ntruncated")
    (tmp_path / "scan_summary.json").write_text(json.dumps({"run_id": "old", "signals": 42}))
    summary = build_summary(tmp_path)
    assert "check-ai" in summary and "Exit code: `0`" in summary
    assert "old-failure" not in summary and "Scan funnel" not in summary


def test_cli_invalid_configuration_produces_safe_failure_diagnostics(
    tmp_path, monkeypatch, logger_state
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIVERSE_SIZE", "not-a-number-private-value")
    assert main(["validate-config"]) == 2
    contents = (tmp_path / "reports" / "runtime.jsonl").read_text()
    assert "not-a-number-private-value" not in contents
    rows = read_runtime(tmp_path / "reports")
    assert rows[-1]["event"] == "run_finished" and rows[-1]["exit_code"] == 2
    failure = next(r for r in rows if r["event"] == "configuration_or_schema_failure")
    assert "universe_size" in failure["fields"]


def test_sector_map_changes_invalidate_cache_and_cached_runs_export_snapshot(tmp_path):
    path = tmp_path / "sectors.json"
    path.write_text(json.dumps({"AAPL": "Information Technology"}))
    settings = cfg(tmp_path, sector_map_path=str(path))
    store = Store(settings.state_path)
    asset = NS(
        symbol="AAPL",
        status="active",
        tradable=True,
        asset_class="us_equity",
        exchange="NASDAQ",
        name="Apple",
    )
    trading = Mock()
    trading.get_all_assets.return_value = [asset]
    data = Mock()
    data.daily.return_value = {"AAPL": frames()[1]}
    universe = Universe(trading, data, settings, store)
    with store.writer():
        first = universe.build(NOW)
        snapshot = settings.reports_dir / "universe.json"
        snapshot.unlink()
        assert universe.build(NOW) == first
        assert json.loads(snapshot.read_text()) == first
        assert trading.get_all_assets.call_count == 1
        path.write_text(json.dumps({"AAPL": "Updated classification"}))
        assert universe.build(NOW)[0]["sector"] == "Updated classification"
        assert trading.get_all_assets.call_count == 2
    store.close()
