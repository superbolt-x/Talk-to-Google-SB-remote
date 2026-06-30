"""Date-range validation and safe window tiling, shared by the read tools.

Both Google Ads (GAQL ``segments.date BETWEEN x AND y``) and GA4 ``DateRange``
are **INCLUSIVE on the start and end date** — ``2026-06-01..2026-06-07`` covers
seven days, not six. This is the source of a subtle reporting bug: when a period
is split into adjacent windows for week-over-week analysis, the next window must
start the day *after* the previous window's end. Reusing an end date as the next
start (``6/1-6/15`` then ``6/15-6/28``) counts the shared day twice, so summing
the windows overstates cost, impressions, and clicks — and the more windows you
split into, the larger the overcount.

``split_date_range`` generates correctly tiled, non-overlapping windows so a
caller never has to reason about the off-by-one.
"""

from __future__ import annotations

from datetime import date, timedelta

# One-line note suitable for embedding in tool docstrings / error hints.
INCLUSIVE_RANGE_NOTE = (
    "Date ranges are inclusive on both ends (YYYY-MM-DD). For multi-window "
    "analysis, build windows with split_date_range so adjacent windows do not "
    "share a boundary day — overlapping windows double-count metrics."
)


def _parse_iso(value: str, label: str) -> date:
    """Parse a YYYY-MM-DD string into a date, with an actionable error message."""
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{label} must be a calendar date in YYYY-MM-DD format, got {value!r}."
        ) from exc


def validate_iso_date_range(start: str, end: str) -> None:
    """Raise ``ValueError`` unless start/end are valid YYYY-MM-DD and start <= end.

    Guards the two silent failure modes of an inclusive date filter:
      * a malformed date (which otherwise errors deep inside the Google Ads API);
      * a reversed range (start > end), which silently returns zero rows and
        reads as "no spend" rather than as a mistake.

    Both bounds are inclusive — see the module docstring for why adjacent
    windows must not share a boundary day.
    """
    start_d = _parse_iso(start, "date_range_start")
    end_d = _parse_iso(end, "date_range_end")
    if start_d > end_d:
        raise ValueError(
            f"date_range_start ({start}) is after date_range_end ({end}); the "
            "range is inclusive and must be ordered start <= end."
        )


def split_date_range(
    start: str, end: str, *, days: int = 7
) -> list[tuple[str, str]]:
    """Split an inclusive [start, end] range into consecutive non-overlapping windows.

    Returns a list of ``(start, end)`` ISO-date tuples, each at most ``days``
    long, tiling the whole range with **no shared boundary days and no gaps**.
    Because the windows do not overlap, summing any additive metric (cost,
    impressions, clicks) across them equals the metric over the whole range.

    Example::

        split_date_range("2026-06-01", "2026-06-28", days=7)
        # [("2026-06-01", "2026-06-07"), ("2026-06-08", "2026-06-14"),
        #  ("2026-06-15", "2026-06-21"), ("2026-06-22", "2026-06-28")]
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}.")
    validate_iso_date_range(start, end)

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    step = timedelta(days=days)

    windows: list[tuple[str, str]] = []
    cursor = start_d
    while cursor <= end_d:
        window_end = min(cursor + step - timedelta(days=1), end_d)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows
