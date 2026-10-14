from datetime import date, datetime
from zoneinfo import ZoneInfo

STORE_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")


class SystemClock:
    def __init__(self, tz: ZoneInfo = STORE_TIMEZONE) -> None:
        self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def today(self) -> date:
        return self.now().date()
