"""Structured runtime traces. Never log provider bodies, credentials, or exception locals."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr

LOGGER = logging.getLogger("swing.diagnostics")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
_context: ContextVar[dict] = ContextVar("diagnostic_context", default={})
_secrets: set[str] = set()
_sensitive = re.compile(
    r"api.?key|secret|password|authorization|credential|database_url|dsn|private_key|"
    r"webhook_url|google_sa_json|^token$|^headers$|^request_body$|^response_body$",
    re.I,
)


def register_secrets(settings):
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr) and value.get_secret_value():
            _secrets.add(value.get_secret_value())


def safe_text(value):
    for secret in sorted(_secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    # Connection strings, URL userinfo, query credentials and webhook paths are private.
    value = re.sub(r"postgres(?:ql)?://[^\s\"'<>]+", "[REDACTED_DATABASE_URL]", value)

    def url(match):
        try:
            parsed = urlsplit(match.group())
            return f"{parsed.scheme}://{parsed.hostname}/[path omitted]"
        except ValueError:
            return "[REDACTED_URL]"

    value = re.sub(r"https?://[^\s\"'<>]+", url, value)
    value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    return value[:4000]


def safe_value(value, depth=0):
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if depth >= 10:
        return "[nested details omitted]"
    if isinstance(value, dict):
        return {
            safe_text(str(k)): "[REDACTED]"
            if _sensitive.search(str(k))
            else safe_value(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [safe_value(v, depth + 1) for v in items[:100]]
        if len(items) > 100:
            result.append({"additional_items": len(items) - 100})
        return result
    if isinstance(value, Path):
        return safe_text(str(value))
    # Never invoke arbitrary object repr(), which may contain credentials or HTTP bodies.
    return f"<{type(value).__name__}>"


def error_details(exc):
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    frames = traceback.extract_tb(exc.__traceback__)
    output = {
        "error_type": type(exc).__name__,
        "http_status": status if isinstance(status, int) else None,
        "trace": [f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames[-12:]],
    }
    # Only our own validation messages are useful/safe to display. Third-party
    # exceptions often contain URLs, SQL connection strings, headers or bodies.
    if isinstance(exc, ValueError) and frames:
        origin = Path(frames[-1].filename)
        if origin.parent == Path(__file__).parent or origin.name == "strategy.py":
            output["reason"] = safe_text(str(exc))
    return output


def run_id():
    return _context.get().get("run_id")


def emit(event, level="DEBUG", **fields):
    try:
        if not LOGGER.isEnabledFor(getattr(logging, level)):
            return
        record = safe_value(
            {
                **_context.get(),
                **fields,
                "at": datetime.now(timezone.utc),
                "level": level,
                "event": event,
            }
        )
        LOGGER.log(getattr(logging, level), json.dumps(record, allow_nan=False))
    except Exception:
        # Optional runtime logging must never turn an accepted order into a retry.
        # Durable business audits retain their existing fail-closed behavior.
        try:
            sys.stderr.write('{"event":"diagnostic_logging_failed","level":"ERROR"}\n')
        except Exception:
            pass


@contextmanager
def context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def span(stage, **fields):
    started = time.monotonic()
    with context(stage=stage, **fields):
        emit("stage_started")
        try:
            yield
        except Exception as exc:
            emit(
                "stage_failed",
                "ERROR",
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                **error_details(exc),
            )
            raise
        else:
            emit("stage_completed", elapsed_ms=round((time.monotonic() - started) * 1000, 2))


def traced(stage):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(stage):
                return fn(*args, **kwargs)

        return wrapped

    return decorate


def configure(settings, mode):
    register_secrets(settings)
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(settings.log_level)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(stream)
    _context.set(
        {
            "run_id": str(uuid.uuid4()),
            "mode": mode,
            "github_run_id": os.getenv("GITHUB_RUN_ID"),
            "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "revision": os.getenv("GITHUB_SHA"),
        }
    )
    try:
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(settings.reports_dir / "runtime.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)
    except OSError as exc:
        emit("runtime_file_unavailable", "WARNING", **error_details(exc))
    safe_config = {
        name: value
        for name, value in settings.model_dump().items()
        if not isinstance(value, SecretStr) and name != "google_sheet_id"
    }
    emit("run_started", "INFO", configuration=safe_config)
    return run_id()


def audit_summary(kind, payload):
    """Keep stdout readable; full business evidence remains in the durable audit."""
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    result = dict(payload)
    for key in (
        "selected",
        "rejected",
        "items",
        "events",
        "news",
        "approvals",
        "candidate",
        "approval",
    ):
        value = result.pop(key, None)
        if isinstance(value, (dict, list)):
            result[key + "_count"] = len(value)
    return result
