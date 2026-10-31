from datetime import UTC, date, datetime, timedelta

import pytest

from tech_researcher.periods import (
    JST,
    daily_target,
    in_utc_date_window,
    last30days_window,
    weekly_target,
)


def jst(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=JST)


def test_daily_target_is_previous_day_half_open():
    target = daily_target(date(2026, 10, 24), period_days=1)
    assert target.key == date(2026, 10, 23)
    assert target.period.start == jst(2026, 10, 23)
    assert target.period.end == jst(2026, 10, 24)
    assert target.period.contains(jst(2026, 10, 23))
    assert target.period.contains(jst(2026, 10, 23, 23, 59))
    assert not target.period.contains(jst(2026, 10, 24))  # 終わりは含まない


def test_contains_compares_instants_across_timezones():
    period = daily_target(date(2026, 10, 24), 1).period
    assert period.contains(datetime(2026, 10, 22, 15, 0, tzinfo=UTC))  # JST 10/23 0:00
    assert not period.contains(datetime(2026, 10, 22, 14, 59, tzinfo=UTC))


def test_contains_rejects_naive_datetime():
    with pytest.raises(ValueError):
        daily_target(date(2026, 10, 24), 1).period.contains(datetime(2026, 10, 23, 12))


def test_weekly_target_is_previous_monday_to_sunday():
    target = weekly_target(date(2026, 10, 26), period_days=7)  # 月曜
    assert target.key == date(2026, 10, 26)
    assert target.period.start == jst(2026, 10, 19)
    assert target.period.end == jst(2026, 10, 26)


def test_weekly_key_is_monday_on_or_before_run_date():
    assert weekly_target(date(2026, 10, 28), 7).key == date(2026, 10, 26)  # 水曜
    assert weekly_target(date(2026, 11, 1), 7).key == date(2026, 10, 26)  # 日曜


def test_period_setting_does_not_change_target_key():
    assert daily_target(date(2026, 10, 24), 3).key == daily_target(date(2026, 10, 24), 1).key
    long_week = weekly_target(date(2026, 10, 26), 14)
    assert long_week.key == date(2026, 10, 26)
    assert long_week.period.end - long_week.period.start == timedelta(days=14)


def test_period_crosses_year_boundary():
    period = daily_target(date(2026, 1, 1), 1).period  # 年をまたぐ
    assert period.start == jst(2025, 12, 31)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "1"])
def test_period_days_must_be_positive_int(bad):
    with pytest.raises(ValueError):
        daily_target(date(2026, 10, 24), bad)


def test_last30days_window_daily():
    # 対象日 T（JST）は --days 1 --as-of T（UTC の T−1〜T）。D-16
    assert last30days_window(daily_target(date(2026, 10, 24), 1).period) == (1, date(2026, 10, 23))


def test_last30days_window_weekly():
    # 月曜 M の週次は --days 7 --as-of M−1（UTC の M−8〜M−1）
    period = weekly_target(date(2026, 10, 26), 7).period
    assert last30days_window(period) == (7, date(2026, 10, 25))


def test_in_utc_date_window_includes_both_ends():
    days, as_of = 1, date(2026, 10, 23)
    assert in_utc_date_window(date(2026, 10, 22), days, as_of)
    assert in_utc_date_window(date(2026, 10, 23), days, as_of)
    assert not in_utc_date_window(date(2026, 10, 21), days, as_of)
    assert not in_utc_date_window(date(2026, 10, 24), days, as_of)


def test_longer_daily_period_keeps_end_and_moves_start():
    target = daily_target(date(2026, 10, 24), 3)
    assert target.period.start == jst(2026, 10, 21)
    assert target.period.end == jst(2026, 10, 24)
    assert last30days_window(target.period) == (3, date(2026, 10, 23))


def test_longer_weekly_period_converts_to_utc_dates():
    # 14日なら UTC の M−15〜M−1 を覆う
    period = weekly_target(date(2026, 10, 26), 14).period
    assert last30days_window(period) == (14, date(2026, 10, 25))
