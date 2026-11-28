import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from trading_bot import cli
from trading_bot.scheduling import claim_slot, finish_slot, publish_receipt, scheduled_request
from trading_bot.state import Store

from .helpers import cfg


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def external(due="2026-11-30T14:33:00Z", **extra):
    return {
        "SCHEDULER_AUTHORITY": "external",
        "EXTERNAL_SCHEDULER_MODE": "observe",
        "SCHEDULED_FOR": due,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "fixture/repository",
        "GITHUB_RUN_ID": "123",
        **extra,
    }


@pytest.mark.parametrize(
    "due",
    [
        "2026-03-06T14:33:00Z",  # EST before spring transition
        "2026-03-09T13:33:00Z",  # EDT after spring transition
        "2026-10-30T13:33:00Z",  # EDT before fall transition
        "2026-11-02T14:33:00Z",  # EST after fall transition
        "2026-11-27T17:48:00Z",  # final slot before Thanksgiving early close
    ],
)
def test_exchange_calendar_handles_dst_and_early_close(due):
    slot, skip = scheduled_request("observe", external(due), instant(due) + timedelta(seconds=30))
    assert skip is None and slot.scheduled_for == instant(due)


@pytest.mark.parametrize(
    "due,now",
    [
        ("2026-11-26T14:33:00Z", "2026-11-26T14:33:10Z"),  # holiday
        ("2026-11-28T14:33:00Z", "2026-11-28T14:33:10Z"),  # weekend
        ("2026-11-27T18:03:00Z", "2026-11-27T18:03:10Z"),  # after early close
        ("2026-11-30T14:30:00Z", "2026-11-30T14:30:10Z"),  # wrong cadence
        ("2026-11-30T14:33:00Z", "2026-11-30T14:32:59Z"),  # future slot
        ("2026-11-30T14:33:00Z", "2026-11-30T14:36:00Z"),  # deadline exclusive
        ("2026-11-30T14:33:00", "2026-11-30T14:33:10Z"),  # naive timestamp
    ],
)
def test_invalid_or_expired_slots_cannot_enter_engine(due, now):
    with pytest.raises(ValueError):
        scheduled_request("observe", external(due), instant(now))


def test_cutover_is_explicit_and_native_deliveries_cannot_be_relabelled():
    now = instant("2026-11-30T14:33:10Z")
    assert scheduled_request("observe", {}, now) == (None, None)
    assert scheduled_request("weekly-report", {"SCHEDULER_AUTHORITY": "external"}, now) == (
        None,
        None,
    )
    assert scheduled_request("paper-execute", external(GITHUB_EVENT_NAME="schedule"), now) == (
        None,
        "NATIVE_SCHEDULE_DISABLED",
    )
    for settings in (
        external(SCHEDULER_AUTHORITY="native"),
        external(SCHEDULER_AUTHORITY="typo"),
        external(SCHEDULED_FOR=""),
        external(EXTERNAL_SCHEDULER_MODE="paper-execute"),
    ):
        with pytest.raises(ValueError):
            scheduled_request("observe", settings, now)
    with pytest.raises(ValueError):
        scheduled_request("paper-execute", external(), now)


@pytest.mark.parametrize("outcome", ["completed", "crashed"])
def test_sequential_retry_after_completion_or_crash_is_rejected(tmp_path, outcome):
    now = instant("2026-11-30T14:33:10Z")
    slot, _ = scheduled_request("observe", external(), now)
    path = tmp_path / "state.db"
    first = Store(path)
    try:
        with first.writer():
            receipt = claim_slot(first, slot, now)
            assert receipt
            if outcome == "completed":
                finish_slot(first, receipt, {"status": "observe", "scanned": 100}, now)
    finally:
        first.close()
    # Different run ID, mode and timezone representation still claim the same slot.
    retry_env = external(
        "2026-11-30T09:33:00-05:00", GITHUB_RUN_ID="456", EXTERNAL_SCHEDULER_MODE="paper-dry-run"
    )
    retry, _ = scheduled_request("paper-dry-run", retry_env, now)
    assert retry.key == slot.key
    second = Store(path)
    try:
        with second.writer():
            assert claim_slot(second, retry, now) is None
            assert second.get(slot.key)["github_run_id"] == "123"
    finally:
        second.close()


def test_recheck_after_queue_delay_and_durable_state_requirement(tmp_path):
    now = instant("2026-11-30T14:33:10Z")
    slot, _ = scheduled_request("observe", external(), now)
    store = Store(tmp_path / "state.db")
    try:
        with pytest.raises(RuntimeError):
            claim_slot(store, slot, now)
        with store.writer():
            with pytest.raises(ValueError, match="expired"):
                claim_slot(store, slot, slot.deadline)
            with pytest.raises(ValueError, match="PostgreSQL"):
                claim_slot(store, slot, now, durable_required=True)
            assert store.get(slot.key) is None
    finally:
        store.close()


def test_failed_monitoring_cannot_remove_execution_claim(tmp_path, monkeypatch):
    now = instant("2026-11-30T14:33:10Z")
    slot, _ = scheduled_request("observe", external(), now)
    store = Store(tmp_path / "state.db")
    post = Mock(side_effect=TimeoutError("private token must not be printed"))
    monkeypatch.setattr("requests.post", post)
    try:
        with store.writer():
            receipt = claim_slot(store, slot, now)
            publish_receipt(
                receipt,
                tmp_path,
                {
                    "SCHEDULER_RECEIPT_URL": "https://fixture.workers.dev/receipt",
                    "SCHEDULER_RECEIPT_TOKEN": "fixture",
                },
            )
            assert claim_slot(store, slot, now) is None
            assert (
                json.loads((tmp_path / "schedule_receipt.json").read_text())["status"] == "STARTED"
            )
            assert post.call_args.kwargs["allow_redirects"] is False
    finally:
        store.close()


@pytest.mark.parametrize("event,expected", [("schedule", 0), ("workflow_dispatch", 2)])
def test_cli_rejects_native_or_expired_external_before_broker_clients(
    tmp_path, monkeypatch, event, expected
):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant("2026-11-30T14:37:00Z").astimezone(tz or timezone.utc)

    settings = cfg(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "configure", lambda *_: None)
    monkeypatch.setattr(cli, "datetime", FixedDateTime)
    broker_clients = Mock(side_effect=AssertionError("Broker must not be touched"))
    monkeypatch.setattr("trading_bot.providers.clients", broker_clients)
    for name, value in external(GITHUB_EVENT_NAME=event).items():
        monkeypatch.setenv(name, value)
    assert cli.main(["observe"]) == expected
    broker_clients.assert_not_called()


def test_cli_does_not_start_pipeline_if_monitoring_consumes_remaining_window(tmp_path, monkeypatch):
    clock = [instant("2026-11-30T14:35:59Z")]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz or timezone.utc)

    settings = cfg(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "configure", lambda *_: None)
    monkeypatch.setattr(cli, "datetime", FixedDateTime)
    trading = Mock(_base_url="https://paper-api.alpaca.markets")
    monkeypatch.setattr("trading_bot.providers.clients", lambda _: (trading, Mock(), Mock()))
    pipeline = Mock()
    monkeypatch.setattr("trading_bot.pipeline.Pipeline", lambda *_: pipeline)

    def delayed_receipt(*_):
        clock[0] += timedelta(seconds=5)

    monkeypatch.setattr("trading_bot.scheduling.publish_receipt", delayed_receipt)
    for name, value in external().items():
        monkeypatch.setenv(name, value)
    assert cli.main(["observe"]) == 0
    pipeline.run.assert_not_called()
    store = Store(settings.state_path)
    try:
        rows = store.events("scheduled_slot_finished")
        assert rows[0]["payload"]["engine_status"] == "STALE_SLOT"
    finally:
        store.close()
