#!/usr/bin/env python3
"""Time-zone validation, filtering, and deterministic conversion checks."""

from datetime import datetime, timezone

from marsv2.timezones import (
    current_time_lines, timezone_matches, timezone_names, validate_timezone,
)


names = timezone_names()
assert names[0] == "UTC"
for required in ("America/New_York", "Europe/London", "Pacific/Honolulu"):
    assert required in names
    assert validate_timezone(required) == required
assert timezone_matches(names, "new_york")[0] == "America/New_York"
assert "Pacific/Honolulu" in timezone_matches(names, "hono")
try:
    validate_timezone("Mars/Olympus_Mons")
    raise AssertionError("unknown zone accepted")
except ValueError:
    pass

instant = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
utc_line, local_line = current_time_lines("America/New_York", instant)
assert "12:00:00 PM UTC" in utc_line and "UTC+0000" in utc_line
assert "07:00:00 AM EST" in local_line and "UTC-0500" in local_line
print(f"PASS {len(names)} validated time zones and UTC/local conversion")
