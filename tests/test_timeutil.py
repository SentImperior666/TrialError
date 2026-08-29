import re
from datetime import timezone

from trialerror.util.timeutil import now, now_dt, parse

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_now_format():
    value = now()
    assert _ISO_RE.match(value), value


def test_now_is_utc_and_parses_back():
    value = now()
    dt = parse(value)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_now_dt_is_timezone_aware_utc():
    dt = now_dt()
    assert dt.tzinfo is timezone.utc


def test_now_non_decreasing_across_calls():
    a = now()
    b = now()
    assert b >= a  # ISO-8601 UTC strings sort lexicographically by time


def test_parse_roundtrip_millisecond_precision():
    value = now()
    dt = parse(value)
    # re-render manually and compare to the original string (millisecond precision preserved)
    rendered = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    assert rendered == value
