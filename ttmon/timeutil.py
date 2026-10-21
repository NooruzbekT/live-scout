from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ttmon.settings import get_config

REPORT_DATE_FORMAT = "%d.%m.%Y"


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_almaty(moment: datetime, tz: ZoneInfo | None = None) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime: время должно быть с часовым поясом")
    return moment.astimezone(tz or get_config().tz)


def now_almaty(tz: ZoneInfo | None = None) -> datetime:
    return to_almaty(now_utc(), tz)


def today_almaty(tz: ZoneInfo | None = None) -> date:
    return now_almaty(tz).date()


def format_report_date(day: date) -> str:
    return day.strftime(REPORT_DATE_FORMAT)
