"""Read GitHub evidence without dispatching workflows or inferring missing firing times."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import yaml

from trading_bot.calendar import schedule

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def instant(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Audit timestamps require a timezone")
    return value.astimezone(UTC)


def stamp(value):
    return {"utc": value.isoformat(), "new_york": value.astimezone(NY).isoformat()}


def api(repository, path):
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/{path}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def field_values(expression, low, high):
    values = set()
    for part in expression.split(","):
        body, slash, step = part.partition("/")
        step = int(step or 1)
        if body == "*":
            first, last = low, high
        elif "-" in body:
            first, last = map(int, body.split("-"))
        else:
            first = int(body)
            last = high if slash else first
        if step <= 0 or not low <= first <= last <= high:
            raise ValueError("Invalid cron field")
        values.update(range(first, last + 1, step))
    return values


def expected_minutes(entries, start, end, bounds=None):
    parsed = []
    for entry in entries:
        minute, hour, dom, month, dow = entry["cron"].split()
        # All schedules in this repo use wildcard day/month. Reject unfamiliar
        # expressions instead of silently misapplying cron's DOM/DOW OR rule.
        if dom != "*" or month != "*":
            raise ValueError("Audit supports wildcard day-of-month and month only")
        parsed.append(
            (
                field_values(minute, 0, 59),
                field_values(hour, 0, 23),
                {day % 7 for day in field_values(dow, 0, 7)},
                ZoneInfo(entry.get("timezone", "UTC")),
            )
        )
    minute = start.replace(second=0, microsecond=0)
    if minute < start:
        minute += timedelta(minutes=1)
    result = []
    while minute <= end:
        for minutes, hours, weekdays, zone in parsed:
            local = minute.astimezone(zone)
            if (
                local.minute in minutes
                and local.hour in hours
                and (local.weekday() + 1) % 7 in weekdays
                and (bounds is None or in_session(minute, bounds))
            ):
                result.append(minute)
                break
        minute += timedelta(minutes=1)
    return result


def in_session(value, bounds):
    session = bounds.get(value.astimezone(NY).date().isoformat())
    return bool(session and session[0] <= value < session[1])


def engine_outcome(repository, run):
    if run["status"] != "completed":
        return {"logs": "not_complete"}
    result = subprocess.run(
        ["gh", "run", "view", str(run["id"]), "--repo", repository, "--log"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        return {"logs": "unavailable"}
    outcome = {}
    for line in result.stdout.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if "event" not in record and "status" in record:
            outcome = {
                key: record[key] for key in ("status", "scanned", "submitted") if key in record
            }
    # Never retain raw provider/account logs or presume execution mode means fills.
    return outcome or {"logs": "no_final_result_found"}


def audit_workflow(repository, filename, revision, start, end, bounds, include_logs):
    metadata = api(repository, f"actions/workflows/{filename}")
    content = api(repository, f"contents/.github/workflows/{filename}?ref={revision}")
    definition = yaml.load(base64.b64decode(content["content"]), Loader=yaml.BaseLoader)
    begin = max(start, instant(metadata["created_at"]))
    engine = filename == "swing-scan.yml"
    expected = expected_minutes(
        definition["on"].get("schedule", []), begin, end, bounds if engine else None
    )
    runs = []
    page = 1
    while True:
        query = urlencode(
            {"created": f"{begin.isoformat()}..{end.isoformat()}", "per_page": 100, "page": page}
        )
        batch = api(repository, f"actions/workflows/{filename}/runs?{query}")["workflow_runs"]
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    actual = []
    for run in runs:
        created = instant(run["created_at"])
        jobs = api(
            repository, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
        )
        starts = sorted(instant(job["started_at"]) for job in jobs["jobs"] if job.get("started_at"))
        first = starts[0] if starts else None
        item = {
            "id": run["id"],
            "url": run["html_url"],
            "event": run["event"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "attempt": run["run_attempt"],
            "created": stamp(created),
            "first_job_started": stamp(first) if first else None,
            "creation_to_job_seconds": (first - created).total_seconds() if first else None,
            "created_in_regular_session": in_session(created, bounds),
            "nominal_trigger_time": None,
            "jobs": [
                {
                    key: job.get(key)
                    for key in ("id", "name", "created_at", "started_at", "conclusion")
                }
                for job in jobs["jobs"]
            ],
        }
        if engine and include_logs:
            item["engine_outcome"] = engine_outcome(repository, run)
        actual.append(item)
    slots = []
    for due in expected:
        close = due + timedelta(minutes=15)
        if engine:
            close = min(close, bounds[due.astimezone(NY).date().isoformat()][1])
        ids = [
            run["id"]
            for run in runs
            if run["event"] == "schedule" and due <= instant(run["created_at"]) < close
        ]
        slots.append(
            {
                **stamp(due),
                "bucket_end": stamp(close),
                "bucket_complete": end >= close,
                "scheduled_run_creations_in_bucket": ids,
            }
        )
    return {
        "workflow": filename,
        "state": metadata["state"],
        "cron": definition["on"].get("schedule", []),
        "expected_regular_session_only": engine,
        "expected_slots": len(slots),
        "scheduled_run_creations": sum(r["event"] == "schedule" for r in actual),
        "completed_buckets_without_run_creation": sum(
            row["bucket_complete"] and not row["scheduled_run_creations_in_bucket"] for row in slots
        ),
        "slots": slots,
        "runs": actual,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="ivssraviraj/alpaca-swing-trading")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", default=datetime.now(UTC).isoformat())
    parser.add_argument("--workflow", action="append")
    parser.add_argument("--include-engine-logs", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start, end = instant(args.since), instant(args.until)
    if start >= end or end - start > timedelta(days=8):
        parser.error("Choose an increasing interval of at most eight days")
    revision = api(args.repo, "branches/main")["commit"]["sha"]
    frame = schedule(str(start.astimezone(NY).date()), str(end.astimezone(NY).date()))
    bounds = {
        str(day.date()): (row.market_open.to_pydatetime(), row.market_close.to_pydatetime())
        for day, row in frame.iterrows()
    }
    report = {
        "repository": args.repo,
        "revision": revision,
        "window": {"start": stamp(start), "end": stamp(end)},
        "interpretation": (
            "Expected slots use the current main revision, starting no earlier than workflow creation. "
            "Historical cron changes require separate windows. Creation buckets are observations, "
            "not proof of nominal trigger identity: GitHub does not expose that timestamp here. "
            "An empty bucket does not establish the internal reason. Job delay is measured only "
            "after run creation; rerun attempts retain the original run creation time."
        ),
        "workflows": [
            audit_workflow(args.repo, name, revision, start, end, bounds, args.include_engine_logs)
            for name in args.workflow
            or [
                "swing-scan.yml",
                "scheduler-heartbeat.yml",
                "scheduler-offset-probe.yml",
                "scheduler-minimal-probe.yml",
            ]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": str(args.output),
                "workflows": [
                    {
                        key: row[key]
                        for key in (
                            "workflow",
                            "state",
                            "expected_slots",
                            "scheduled_run_creations",
                            "completed_buckets_without_run_creation",
                        )
                    }
                    for row in report["workflows"]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
