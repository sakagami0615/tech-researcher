"""日次・週次の対象と調査期間の計算（structure.md 5章、D-16・D-17）。

期間は JST の半開区間 [start, end) で表す。対象の識別（日次は対象日、週次は対象週の月曜）は、
調査期間の長さの設定に左右されない（US-E4-03）。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError("タイムゾーンのない日時は判定できない")
        return self.start <= moment < self.end


@dataclass(frozen=True)
class Target:
    """1回の実行の対象。key は日次なら対象日、週次なら対象週の月曜。"""

    cadence: str
    key: date
    period: Period


def jst_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=JST)


def daily_target(run_date: date, period_days: int) -> Target:
    """実行日（JST）の日次の対象。対象日は実行日の前日、期間は実行日 0:00 までの N 日。"""
    _check_days(period_days)
    end = jst_midnight(run_date)
    return Target(
        "daily", run_date - timedelta(days=1), Period(end - timedelta(days=period_days), end)
    )


def weekly_target(run_date: date, period_days: int) -> Target:
    """実行日（JST）の週次の対象。

    対象週はその日以前で最も近い月曜、期間はその月曜 0:00 までの N 日。
    """
    _check_days(period_days)
    monday = run_date - timedelta(days=run_date.weekday())
    end = jst_midnight(monday)
    return Target("weekly", monday, Period(end - timedelta(days=period_days), end))


def last30days_window(period: Period) -> tuple[int, date]:
    """last30days-skill に渡す --days と --as-of（D-16）。期間を覆う UTC の日付の範囲を返す。"""
    as_of = period.end.astimezone(UTC).date()
    return (as_of - period.start.astimezone(UTC).date()).days, as_of


def in_utc_date_window(published: date, days: int, as_of: date) -> bool:
    """UTC の日付だけの記事が、--days と --as-of の範囲（両端を含む）に入るか（D-16）。"""
    return as_of - timedelta(days=days) <= published <= as_of


def _check_days(period_days: int) -> None:
    if isinstance(period_days, bool) or not isinstance(period_days, int) or period_days < 1:
        raise ValueError(f"period_days は1以上の整数にする: {period_days!r}")


def spike_window(target: Target) -> Period:
    """急上昇・新しい OSS の判定の窓（data-model.md 2.4）。対象日 6:00 から翌日 6:00（JST）。"""
    start = datetime.combine(target.key, time(6), tzinfo=JST)
    return Period(start, start + timedelta(days=1))
