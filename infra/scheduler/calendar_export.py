"""Generate the Worker's UTC session table using the engine's pinned NYSE calendar."""

import json
from pathlib import Path

from trading_bot.calendar import schedule

START, END = "2026-01-01", "2027-12-31"
sessions = {
    str(day.date()): [
        int(row.market_open.timestamp() * 1000),
        int(row.market_close.timestamp() * 1000),
    ]
    for day, row in schedule(START, END).iterrows()
}
output = {
    "valid_from": START,
    "valid_through": END,
    "sessions": sessions,
}
Path(__file__).with_name("calendar.json").write_text(json.dumps(output, indent=2) + "\n")
