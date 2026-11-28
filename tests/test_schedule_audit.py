import base64

from scripts import audit_schedule as audit
from trading_bot.calendar import schedule


def test_five_minute_creation_windows_never_count_a_run_twice(monkeypatch):
    run = {
        "id": 123,
        "html_url": "https://fixture.invalid/run/123",
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "created_at": "2026-11-28T20:08:00Z",
    }
    definition = b"on:\n  schedule:\n    - cron: '2/5 * * * *'\n"

    def fixture_api(_repository, path):
        if path == "actions/workflows/probe.yml":
            return {"state": "active", "created_at": "2026-11-28T20:00:00Z"}
        if path.startswith("contents/"):
            return {"content": base64.b64encode(definition).decode()}
        if path.startswith("actions/workflows/probe.yml/runs?"):
            return {"workflow_runs": [run]}
        if path.startswith("actions/runs/123/attempts/1/jobs?"):
            return {"jobs": [{"started_at": "2026-11-28T20:08:02Z"}]}
        raise AssertionError(path)

    monkeypatch.setattr(audit, "api", fixture_api)
    result = audit.audit_workflow(
        "fixture/repo",
        "probe.yml",
        "revision",
        audit.instant("2026-11-28T20:00:00Z"),
        audit.instant("2026-11-28T20:16:00Z"),
        {},
        False,
    )
    assert result["expected_slots"] == 3
    assert [slot["scheduled_run_creations_in_bucket"] for slot in result["slots"]] == [
        [],
        [123],
        [],
    ]
    assert result["slots"][0]["bucket_end"]["utc"] == "2026-11-28T20:07:00+00:00"
    assert result["slots"][-1]["bucket_end"]["utc"] == "2026-11-28T20:17:00+00:00"
    assert result["completed_buckets_without_run_creation"] == 1
    assert result["runs"][0]["creation_to_job_seconds"] == 2
    assert result["runs"][0]["nominal_trigger_time"] is None


def test_audit_honors_thanksgiving_and_early_close():
    frame = schedule("2026-11-23", "2026-11-27")
    bounds = {
        str(day.date()): (row.market_open.to_pydatetime(), row.market_close.to_pydatetime())
        for day, row in frame.iterrows()
    }
    slots = audit.expected_minutes(
        [{"cron": "3,18,33,48 13-22 * * 1-5"}],
        audit.instant("2026-11-22T00:00:00-05:00"),
        audit.instant("2026-11-29T00:00:00-05:00"),
        bounds,
    )
    assert len(slots) == 92
    assert not any(slot.date().isoformat() == "2026-11-26" for slot in slots)
    friday = [slot for slot in slots if slot.date().isoformat() == "2026-11-27"]
    assert len(friday) == 14
    assert friday[0] == audit.instant("2026-11-27T09:33:00-05:00")
    assert friday[-1] == audit.instant("2026-11-27T12:48:00-05:00")
