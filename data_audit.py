"""Conservative intraday data-quality diagnostics, independent of Alpaca SDK.

The expected 78 five-minute bars/day is an approximation for a FULL US
regular session. IEX is one exchange and may omit intervals with no prints.
Holidays, half-days, and partial first/last days are not fully exchange-calendar
adjusted. This audit flags concerns; it does not invent missing bars.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Mapping

import pandas as pd

from trading_bot.diagnostics import emit, traced

REGULAR_BARS_PER_SESSION = 78


@traced("legacy.data_coverage")
def session_coverage(frames: Mapping[str, pd.DataFrame], output_dir: str) -> dict:
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    daily_rows = []
    result = {}
    for symbol, frame in frames.items():
        if frame.empty:
            result[symbol] = {"status": "MISSING", "bars": 0, "coverage_estimate": 0.0}
            continue
        if frame.index.tz is None:
            raise ValueError("Intraday frame index must be timezone aware")
        frame = frame.sort_index()
        local_index = frame.index.tz_convert("America/New_York")
        calendar = pd.Series(1, index=pd.Index(local_index.date)).groupby(level=0).sum()
        first_day, last_day = local_index[0].date(), local_index[-1].date()
        weekdays = pd.bdate_range(first_day, last_day)
        # Partial start/end days and exchange half-days can lower this estimate.
        coverage = (
            min(1.0, len(frame) / (len(weekdays) * REGULAR_BARS_PER_SESSION))
            if len(weekdays)
            else 0.0
        )
        full_days = sum(int(count >= 70) for count in calendar.values)
        incomplete_days = sum(int(count < 60) for count in calendar.values)
        intra_session_gaps = 0
        for day in weekdays:
            date_key = day.date()
            current = local_index[local_index.date == date_key]
            count = len(current)
            gaps = 0
            if count > 1:
                deltas = current[1:] - current[:-1]
                gaps = sum(int(d > pd.Timedelta(minutes=5)) for d in deltas)
            intra_session_gaps += gaps
            daily_rows.append(
                {
                    "symbol": symbol,
                    "date": date_key.isoformat(),
                    "bars": count,
                    "approx_regular_session_slots": REGULAR_BARS_PER_SESSION,
                    "coverage_estimate": round(count / REGULAR_BARS_PER_SESSION, 3),
                    "gaps_gt_5m": gaps,
                }
            )
        result[symbol] = {
            "status": "REVIEW_REQUIRED"
            if (coverage < 0.8 or incomplete_days > 0 or intra_session_gaps > 0)
            else "APPROX_OK",
            "bars": len(frame),
            "observed_days": len(calendar),
            "weekdays_in_observed_span": len(weekdays),
            "coverage_estimate": round(coverage, 4),
            "median_bars_per_observed_day": round(float(calendar.median()), 1),
            "days_below_60_bars": incomplete_days,
            "days_at_least_70_bars": full_days,
            "intra_session_gaps_gt_5m": int(intra_session_gaps),
            "first_bar": frame.index[0].isoformat(),
            "last_bar": frame.index[-1].isoformat(),
        }
    with (dest / "data_coverage_by_day.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "symbol",
                "date",
                "bars",
                "approx_regular_session_slots",
                "coverage_estimate",
                "gaps_gt_5m",
            ],
        )
        writer.writeheader()
        writer.writerows(daily_rows)
    emit("legacy_coverage_completed", symbols=len(frames), coverage=result)
    return result


def next_bar_eligible(detected: pd.Timestamp, next_time: pd.Timestamp) -> bool:
    """Avoid simulating next-bar fills on stale signals after intraday gaps.

    A signal from the last 15 min of one session may fill early next session;
    otherwise the next expected bar must arrive within 10 minutes.
    """
    if next_time <= detected:
        return False
    ny_detected = detected.tz_convert("America/New_York")
    ny_next = next_time.tz_convert("America/New_York")
    if ny_detected.date() == ny_next.date():
        return next_time - detected <= pd.Timedelta(minutes=10)
    from_close = ny_detected.hour == 15 and ny_detected.minute >= 45
    near_open = ny_next.hour == 9 and ny_next.minute <= 45
    calendar_days = (ny_next.date() - ny_detected.date()).days
    return bool(from_close and near_open and 1 <= calendar_days <= 4)
