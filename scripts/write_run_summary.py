"""Produce a concise GitHub summary from this invocation's structured diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path


def cell(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\n", " ")[:500]
    )


def build_summary(directory: Path):
    records = []
    runtime = directory / "runtime.jsonl"
    if runtime.exists():
        for line in runtime.open():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "run_started":
                records = []
            records.append(row)
    start = next((r for r in records if r.get("event") == "run_started"), {})
    run_id = start.get("run_id")
    records = [r for r in records if r.get("run_id") == run_id]
    finish = next((r for r in reversed(records) if r.get("event") == "run_finished"), {})
    rows = [
        "## Swing engine run",
        "",
        f"- Mode: `{cell(start.get('mode', 'unavailable'))}`",
        f"- Run ID: `{cell(run_id or 'unavailable')}`",
        f"- Exit code: `{cell(finish.get('exit_code', 'not recorded'))}`",
        f"- Duration: {cell(finish.get('elapsed_seconds', 'unknown'))} seconds",
        "",
    ]
    summary_path = directory / "scan_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    if run_id and summary.get("run_id") == run_id:
        rows += ["### Scan funnel", "", "| Metric | Count |", "|---|---:|"]
        for key in (
            "universe_selected",
            "scanned",
            "signals",
            "shortlisted",
            "ai_review_attempts",
            "risk_approved",
            "conditional_setups",
            "submitted",
            "unknown_sectors",
            "pattern_evaluated",
            "pattern_matched_symbols",
        ):
            rows.append(f"| {key} | {cell(summary.get(key, 0))} |")
        rows += ["", "### First scanner rejection", "", "| Reason | Symbols |", "|---|---:|"]
        for reason, count in sorted(summary.get("rejection_counts", {}).items()):
            rows.append(f"| {cell(reason)} | {cell(count)} |")
        rows += [
            "",
            "These are first-failure counts, not independent causes. Pattern matches are counted separately; a symbol can fail both a setup and a risk check.",
            "",
        ]
        rows += ["### Setup checks", "", "| Pattern | Symbols passing |", "|---|---:|"]
        for setup, count in sorted(summary.get("setup_pass_counts", {}).items()):
            rows.append(f"| {cell(setup)} | {cell(count)} |")
        rows += ["", "Pattern checks can overlap and do not imply an approved candidate.", ""]
        if summary.get("candidate_rejection_counts"):
            rows += ["### Shortlisted candidate rejections", "", "| Stage | Count |", "|---|---:|"]
            for stage, count in sorted(summary["candidate_rejection_counts"].items()):
                rows.append(f"| {cell(stage)} | {cell(count)} |")
            rows.append("")
        if summary.get("unknown_sectors"):
            rows += [
                "**Sector mapping is incomplete.** Unknown symbols share one concentration bucket. Configure a verified `SECTOR_MAP_PATH`; sectors are never guessed.",
                "",
            ]
    errors = [r for r in records if r.get("level") in {"WARNING", "ERROR"}]
    if errors:
        rows += ["### Recent warnings / failures", "", "| Stage / event | Detail |", "|---|---|"]
        for row in errors[-15:]:
            details = row.get("details", row)
            detail = (
                details.get("reason")
                or details.get("error_type")
                or details.get("explanation")
                or details.get("reasons")
                or "See runtime.jsonl"
            )
            rows.append(f"| {cell(details.get('stage', row.get('event')))} | {cell(detail)} |")
    rows += [
        "",
        "Download the audit artifact for `runtime.jsonl`, `audit.jsonl`, and (when a scan completes with debug traces enabled) `scan_diagnostics.json`.",
        "",
    ]
    return "\n".join(rows)


def main():
    directory = Path("reports")
    directory.mkdir(parents=True, exist_ok=True)
    text = build_summary(directory)
    (directory / "run_summary.md").write_text(text)
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a") as stream:
            stream.write(text)
    print(text)


if __name__ == "__main__":
    main()
