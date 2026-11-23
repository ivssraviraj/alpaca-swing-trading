from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

NY = "America/New_York"


@lru_cache(maxsize=256)
def schedule(start: str, end: str):
    return mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)


def session_bounds(now: datetime):
    day = pd.Timestamp(now).tz_convert(NY).date().isoformat()
    frame = schedule(day, day)
    return None if frame.empty else (frame.iloc[0].market_open, frame.iloc[0].market_close)


def entry_session(now, close_buffer=25):
    bounds = session_bounds(now)
    return bool(
        bounds
        and bounds[0] + timedelta(minutes=15) <= now < bounds[1] - timedelta(minutes=close_buffer)
    )


def held_sessions(opened, now):
    start = pd.Timestamp(opened).tz_convert(NY).date()
    end = pd.Timestamp(now).tz_convert(NY).date()
    return max(0, len(schedule(str(start), str(end))) - 1)


def regular_completed(frame, now, grace_seconds=30):
    if frame.empty:
        return frame.copy()
    if (
        not isinstance(frame.index, pd.DatetimeIndex)
        or frame.index.tz is None
        or frame.index.has_duplicates
        or not frame.index.is_monotonic_increasing
    ):
        raise ValueError("Bars must have unique, ordered, timezone-aware timestamps")
    output = frame.copy()
    output.index = output.index.tz_convert("UTC")
    first = output.index[0].tz_convert(NY).date()
    last = output.index[-1].tz_convert(NY).date()
    valid = pd.Series(False, index=output.index)
    for _, row in schedule(str(first), str(last)).iterrows():
        valid |= (output.index >= row.market_open) & (
            output.index + pd.Timedelta(minutes=5) <= row.market_close
        )
    valid &= output.index + pd.Timedelta(minutes=5, seconds=grace_seconds) <= now
    return output.loc[valid]


def coverage(frame):
    if frame.empty:
        return {"bars": 0, "expected": 0, "fraction": 0.0}
    start, end = frame.index.min(), frame.index.max()
    expected = 0
    for _, row in schedule(
        str(start.tz_convert(NY).date()), str(end.tz_convert(NY).date())
    ).iterrows():
        first, last = (
            max(start, row.market_open),
            min(end, row.market_close - pd.Timedelta(minutes=5)),
        )
        if last >= first:
            expected += int((last - first).total_seconds() // 300) + 1
    return {
        "bars": len(frame),
        "expected": expected,
        "fraction": min(1.0, len(frame) / expected) if expected else 0.0,
    }
