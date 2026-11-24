from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .diagnostics import configure, emit, error_details, traced


def main(argv=None):
    started = time.monotonic()
    code = 1
    try:
        code = _main(argv)
        return code
    finally:
        emit(
            "run_finished",
            "INFO" if code == 0 else "ERROR",
            exit_code=code,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Paper-only AI-reviewed swing trading")
    parser.add_argument(
        "mode",
        choices=[
            "test",
            "validate-config",
            "observe",
            "paper-dry-run",
            "paper-execute",
            "replay",
            "weekly-report",
            "check-alpaca",
            "check-ai",
            "paper-order-smoke",
        ],
    )
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--strategy", choices=["technical", "event", "ai"], default="technical")
    parser.add_argument("--ack", default="")
    parser.add_argument(
        "--compare", action="store_true", help="Compare technical/event/AI and cost sensitivity"
    )
    args = parser.parse_args(argv)
    if args.mode == "test":
        configure(Settings.model_construct(), args.mode)
        return subprocess.call([sys.executable, "-m", "pytest"])
    configured = False
    try:
        cfg = Settings()
        configure(cfg, args.mode)
        configured = True
        if args.mode == "validate-config":
            print(
                json.dumps(
                    {
                        "status": "VALID",
                        "paper_orders_enabled": cfg.paper_orders_enabled,
                        "ai_enabled": cfg.ai_enabled,
                        "feed": cfg.data_feed,
                        "universe_size": cfg.universe_size,
                    }
                )
            )
            return 0
        if args.mode == "replay" and args.fixture:
            from .research import compare, fixture_frames, replay

            intra, daily = fixture_frames(args.fixture)
            archive = json.loads(args.archive.read_text()) if args.archive else None
            result = (
                compare(intra, daily, cfg, archive)
                if args.compare
                else replay(intra, daily, cfg, args.strategy, archive)
            )
            print(json.dumps(result, default=str))
            return 0
        if args.mode == "check-ai":
            return check_ai(cfg)
        from .ai import AIService
        from .evidence import EvidenceService
        from .execution import Broker
        from .pipeline import Pipeline
        from .providers import MarketData, clients
        from .reporting import Reporter
        from .state import Store
        from .universe import Universe

        if args.mode == "paper-execute":
            cfg.execution_gate(args.mode)
        if args.mode == "paper-order-smoke":
            # Full normal gates, technical signal, evidence and AI approval still apply.
            if args.ack != "SUBMIT_TINY_PAPER_ORDER":
                raise ValueError(
                    "Smoke test requires --ack SUBMIT_TINY_PAPER_ORDER plus all execution gates"
                )
            cfg = Settings(max_order_notional=cfg.smoke_test_notional_cap, max_entries_per_scan=1)
            cfg.execution_gate("paper-execute")
            args.mode = "paper-execute"
        trading, market, news = clients(cfg)
        store = Store(
            cfg.state_path,
            cfg.database_url.get_secret_value(),
            debug_traces_enabled=cfg.debug_traces_enabled,
        )
        try:
            with store.writer():
                store.trace(
                    "workflow_started",
                    {
                        "mode": args.mode,
                        "feed": cfg.data_feed,
                        "universe_mode": cfg.universe_mode,
                        "universe_size": cfg.universe_size,
                        "ai_enabled": cfg.ai_enabled,
                        "paper_orders_enabled": cfg.paper_orders_enabled,
                    },
                )
                data = MarketData(market, cfg, store)
                now = datetime.now(timezone.utc)
                if args.mode == "check-alpaca":
                    account, clock = trading.get_account(), trading.get_clock()
                    frames = data.bars(["SPY"], now - timedelta(days=7), now)
                    if not frames or frames["SPY"].empty:
                        raise ValueError("No market data returned for configured feed")
                    emit(
                        "alpaca_connectivity_passed",
                        "INFO",
                        paper=True,
                        account_status=str(account.status),
                        market_open=clock.is_open,
                        bars=len(frames["SPY"]),
                        feed=cfg.data_feed,
                    )
                    print(
                        json.dumps(
                            {
                                "status": "PASS",
                                "paper": True,
                                "account_status": str(account.status),
                                "market_open": clock.is_open,
                                "feed_verified": cfg.data_feed,
                                "bars": len(frames["SPY"]),
                            }
                        )
                    )
                    return 0
                if args.mode == "replay":
                    from .research import compare, replay

                    if not 10 <= args.days <= 2000:
                        raise ValueError("Replay --days must be between 10 and 2000")
                    symbols = [s.strip() for s in cfg.symbols.split(",") if s.strip()]
                    intra = data.bars(symbols, now - timedelta(days=args.days), now)
                    daily = data.bars(
                        symbols, now - timedelta(days=args.days + 130), now, daily=True
                    )
                    if not intra:
                        raise ValueError("No replay market data")
                    archive = json.loads(args.archive.read_text()) if args.archive else None
                    result = (
                        compare(intra, daily, cfg, archive)
                        if args.compare
                        else replay(intra, daily, cfg, args.strategy, archive)
                    )
                    print(json.dumps(result, default=str))
                    return 0
                reporter = Reporter(cfg, store)
                broker = Broker(trading, cfg, store, args.mode, reporter.notify)
                pipeline = Pipeline(
                    cfg,
                    store,
                    broker,
                    data,
                    Universe(trading, data, cfg, store),
                    EvidenceService(news, cfg, store),
                    AIService(cfg, store),
                    reporter,
                )
                result = pipeline.run(args.mode)
                print(json.dumps(result, default=str))
        finally:
            try:
                store.export(cfg.reports_dir)
            finally:
                store.close()
        return 0
    except ValidationError as exc:
        if not configured:
            # Defaults only: do not read the invalid environment again or log its values.
            configure(Settings.model_construct(), args.mode)
        emit(
            "configuration_or_schema_failure",
            "ERROR",
            fields=[".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)],
        )
        # Do not print Pydantic input values: configuration may contain secrets.
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "error": "Invalid configuration/schema",
                    "fields": [".".join(map(str, e["loc"])) for e in exc.errors()],
                }
            )
        )
        return 2
    except Exception as exc:
        details = error_details(exc)
        emit("workflow_failed", "ERROR", **details)
        # Provider exceptions can include URLs/headers; print no exception payload.
        print(
            json.dumps(
                {
                    "status": "BLOCKED" if isinstance(exc, ValueError) else "FAIL_CLOSED",
                    "error_type": type(exc).__name__,
                    "reason": details.get(
                        "reason", "Provider/runtime failure; inspect runtime.jsonl"
                    ),
                }
            )
        )
        return 2


@traced("workflow.check_ai")
def check_ai(cfg):
    if not cfg.openai_api_key.get_secret_value():
        emit("ai_connectivity_blocked", "WARNING", reason="Missing OPENAI_API_KEY")
        print(json.dumps({"status": "BLOCKED", "reason": "Missing OPENAI_API_KEY"}))
        return 2
    if cfg.github_actions and not cfg.database_url.get_secret_value():
        raise ValueError("Scheduled AI checks require durable DATABASE_URL")
    from openai import OpenAI

    from .models import Decision
    from .state import Store

    store = Store(
        cfg.state_path,
        cfg.database_url.get_secret_value(),
        debug_traces_enabled=cfg.debug_traces_enabled,
    )
    try:
        with store.writer():
            store.trace("workflow_started", {"mode": "check-ai", "model": cfg.openai_model})
            now = datetime.now(timezone.utc)
            prompt = (
                "Connectivity test only. Synthetic ticker TEST; no verified news. "
                "Return UNCERTAIN with no evidence, missing_information explaining no verified news, confidence zero, "
                "approval_expires_at 2000-01-01T00:00:00Z. This cannot authorize any order."
            )
            reserve = (
                (
                    len(prompt.encode())
                    + len(json.dumps(Decision.model_json_schema()).encode())
                    + 4096
                )
                * cfg.ai_input_usd_per_million
                + 500 * cfg.ai_output_usd_per_million
            ) / 1000000
            if not store.reserve_ai(
                now,
                reserve,
                cfg.ai_daily_budget_usd,
                cfg.ai_max_calls_daily,
                cfg.ai_min_interval_seconds,
            ):
                raise ValueError("AI connectivity budget exhausted or throttled")
            client = OpenAI(
                api_key=cfg.openai_api_key.get_secret_value(),
                base_url="https://api.openai.com/v1",
                timeout=cfg.request_timeout_seconds,
                max_retries=0,
            )
            result = client.responses.parse(
                model=cfg.openai_model,
                text_format=Decision,
                max_output_tokens=500,
                store=False,
                input=prompt,
            )
            if (
                result.status != "completed"
                or result.output_parsed is None
                or result.output_parsed.decision != "UNCERTAIN"
            ):
                raise ValueError("AI connectivity schema/safety check failed")
            usage = result.usage.model_dump() if result.usage else None
            store.audit(
                "ai_connectivity",
                {"model": cfg.openai_model, "usage": usage, "reserved_usd": reserve},
                now,
            )
            print(json.dumps({"status": "PASS", "model": cfg.openai_model, "usage": usage}))
    finally:
        try:
            store.export(cfg.reports_dir)
        finally:
            store.close()
    return 0
