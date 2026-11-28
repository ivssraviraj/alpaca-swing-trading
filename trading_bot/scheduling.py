"""Admission for external clock slots; never changes trading decisions or order rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .calendar import session_bounds
from .diagnostics import emit

SCAN_MODES = {"observe", "paper-dry-run", "paper-execute"}
START_WINDOW_SECONDS = 180


@dataclass(frozen=True)
class ScheduledSlot:
    key: str
    scheduled_for: datetime
    deadline: datetime
    run_id: str


def scheduled_request(mode, environ, now):
    """Keep native behavior until explicit cutover; reject ambiguous external requests."""
    authority = environ.get("SCHEDULER_AUTHORITY", "native")
    if authority not in {"native", "external"}:
        raise ValueError("Unknown scheduler authority")
    raw = environ.get("SCHEDULED_FOR", "")
    if mode not in SCAN_MODES:
        if raw:
            raise ValueError("Scheduled slots only support scan modes")
        return None, None
    if authority == "external" and environ.get("GITHUB_EVENT_NAME") == "schedule":
        # Native events do not carry a trustworthy nominal firing timestamp.
        return None, "NATIVE_SCHEDULE_DISABLED"
    if not raw:
        if authority == "external":
            raise ValueError("External scheduling requires scheduled_for")
        return None, None
    if authority != "external":
        raise ValueError("External scheduler has not been activated")
    allowed = environ.get("EXTERNAL_SCHEDULER_MODE", "observe")
    if allowed not in SCAN_MODES or mode != allowed:
        raise ValueError("Requested mode differs from external scheduler allowed mode")
    due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if due.tzinfo is None:
        raise ValueError("scheduled_for must include a timezone")
    due = due.astimezone(timezone.utc)
    bounds = session_bounds(due)
    if not bounds:
        raise ValueError("Scheduled slot is not an exchange session")
    opened, closed = (bound.to_pydatetime() for bound in bounds)
    offset = (due - opened - timedelta(minutes=3)).total_seconds()
    if offset < 0 or offset % 900 or due >= closed:
        raise ValueError("Scheduled slot is not on the regular-session cadence")
    deadline = min(due + timedelta(seconds=START_WINDOW_SECONDS), closed)
    if not due <= now < deadline:
        raise ValueError("Scheduled slot is stale or future dated")
    canonical = due.isoformat(timespec="seconds")
    repository = environ.get("GITHUB_REPOSITORY", "local/alpaca-swing-trading")
    return ScheduledSlot(
        key=f"scheduled-slot:{repository}:swing-scan:{canonical}",
        scheduled_for=due,
        deadline=deadline,
        run_id=environ.get("GITHUB_RUN_ID", "local"),
    ), None


def claim_slot(store, slot, now, *, durable_required=False):
    """Commit the claim before any broker call; it survives failures and restarts."""
    store.require_lock()
    if durable_required and not store.postgres:
        raise ValueError("External scheduled runs require durable PostgreSQL state")
    if not slot.scheduled_for <= now < slot.deadline:
        raise ValueError("Scheduled slot expired before engine admission")
    receipt = {
        "slot_key": slot.key,
        "scheduled_for": slot.scheduled_for.isoformat(),
        "deadline": slot.deadline.isoformat(),
        "github_run_id": slot.run_id,
        "engine_started_at": now.isoformat(),
        "status": "STARTED",
    }
    result = store.execute(
        "INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
        (slot.key, json.dumps(receipt, sort_keys=True)),
    )
    if result.rowcount != 1:
        store.audit("scheduled_slot_duplicate", {"slot_key": slot.key}, now)
        return None
    store.audit("scheduled_slot_started", receipt, now)
    return receipt


def finish_slot(store, receipt, result, now):
    updated = {
        **receipt,
        "status": "COMPLETED",
        "finished_at": now.isoformat(),
        "engine_status": result.get("status"),
        "scanned": result.get("scanned"),
    }
    store.put(receipt["slot_key"], updated)
    store.audit("scheduled_slot_finished", updated, now)
    return updated


def publish_receipt(receipt, directory, environ):
    """A failed monitoring callback cannot undo an already committed execution claim."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schedule_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    url = environ.get("SCHEDULER_RECEIPT_URL", "")
    token = environ.get("SCHEDULER_RECEIPT_TOKEN", "")
    if not url or not token:
        emit("scheduler_receipt_callback_missing", "WARNING")
        return
    try:
        from urllib.parse import urlparse

        import requests

        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Invalid scheduler receipt endpoint")
        response = requests.post(
            url,
            json=receipt,
            headers={"Authorization": "Bearer " + token},
            timeout=5,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise ValueError("Scheduler receipt was not acknowledged")
    except Exception as exc:
        emit("scheduler_receipt_callback_failed", "WARNING", error_type=type(exc).__name__)
