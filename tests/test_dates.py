"""Tests for inclusive date-range validation and non-overlapping window tiling."""

from datetime import date, timedelta

import pytest

from adloop.dates import split_date_range, validate_iso_date_range


class TestValidateIsoDateRange:
    def test_allows_ordered_range(self):
        validate_iso_date_range("2026-06-01", "2026-06-28")

    def test_allows_single_day(self):
        validate_iso_date_range("2026-06-15", "2026-06-15")

    def test_rejects_reversed_range(self):
        with pytest.raises(ValueError, match="after date_range_end"):
            validate_iso_date_range("2026-06-28", "2026-06-01")

    def test_rejects_malformed_start(self):
        with pytest.raises(ValueError, match="date_range_start"):
            validate_iso_date_range("06/01/2026", "2026-06-28")

    def test_rejects_malformed_end(self):
        with pytest.raises(ValueError, match="date_range_end"):
            validate_iso_date_range("2026-06-01", "not-a-date")


class TestSplitDateRange:
    def test_tiles_june_into_four_weeks(self):
        windows = split_date_range("2026-06-01", "2026-06-28", days=7)
        assert windows == [
            ("2026-06-01", "2026-06-07"),
            ("2026-06-08", "2026-06-14"),
            ("2026-06-15", "2026-06-21"),
            ("2026-06-22", "2026-06-28"),
        ]

    def test_tiles_june_into_two_halves(self):
        windows = split_date_range("2026-06-01", "2026-06-28", days=14)
        assert windows == [
            ("2026-06-01", "2026-06-14"),
            ("2026-06-15", "2026-06-28"),
        ]

    def test_windows_are_contiguous_and_non_overlapping(self):
        # The next window must start exactly one day after the previous end —
        # this is the property that prevents boundary double-counting.
        windows = split_date_range("2026-06-01", "2026-06-30", days=7)
        for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
            gap = date.fromisoformat(next_start) - date.fromisoformat(prev_end)
            assert gap == timedelta(days=1)

    def test_covers_every_day_exactly_once(self):
        # No gaps and no overlaps: the union of all window days equals the range.
        windows = split_date_range("2026-06-01", "2026-06-28", days=7)
        covered = []
        for start, end in windows:
            d = date.fromisoformat(start)
            last = date.fromisoformat(end)
            while d <= last:
                covered.append(d)
                d += timedelta(days=1)
        assert len(covered) == len(set(covered))  # no day counted twice
        assert covered[0] == date(2026, 6, 1)
        assert covered[-1] == date(2026, 6, 28)
        assert len(covered) == 28

    def test_last_window_clamped_to_end(self):
        # Range not a multiple of `days`: final window is short, never past end.
        windows = split_date_range("2026-06-01", "2026-06-10", days=7)
        assert windows == [
            ("2026-06-01", "2026-06-07"),
            ("2026-06-08", "2026-06-10"),
        ]

    def test_single_day_range(self):
        assert split_date_range("2026-06-15", "2026-06-15", days=7) == [
            ("2026-06-15", "2026-06-15")
        ]

    def test_rejects_non_positive_days(self):
        with pytest.raises(ValueError, match="days must be >= 1"):
            split_date_range("2026-06-01", "2026-06-28", days=0)

    def test_rejects_reversed_range(self):
        with pytest.raises(ValueError, match="after date_range_end"):
            split_date_range("2026-06-28", "2026-06-01", days=7)
