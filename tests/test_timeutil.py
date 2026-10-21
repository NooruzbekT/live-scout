from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from ttmon.timeutil import format_report_date, now_almaty, to_almaty

ALMATY = ZoneInfo("Asia/Almaty")


def test_utc_evening_is_next_day_in_almaty() -> None:
    local = to_almaty(datetime(2026, 10, 15, 20, 0, tzinfo=UTC), ALMATY)
    assert local == datetime(2026, 10, 16, 1, 0, tzinfo=ALMATY)
    assert local.utcoffset() is not None
    assert local.utcoffset().total_seconds() == 5 * 3600  # type: ignore[union-attr]


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_almaty(datetime(2026, 10, 15, 10, 0), ALMATY)


def test_now_almaty_uses_config_timezone() -> None:
    assert now_almaty().tzinfo == ALMATY


def test_report_date_format() -> None:
    assert format_report_date(date(2026, 1, 5)) == "05.01.2026"
