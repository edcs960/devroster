"""Korean timestamps also work on systems without an IANA timezone database."""

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def korean_timezone() -> tzinfo:
    try:
        return ZoneInfo("Asia/Seoul")
    except ZoneInfoNotFoundError:
        # Current Korean time is UTC+09:00 with no daylight-saving transitions.
        return timezone(timedelta(hours=9), "Asia/Seoul")


KST = korean_timezone()
