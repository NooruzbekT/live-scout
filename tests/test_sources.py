from datetime import date, timedelta

import pytest

from ttmon.collector.sources import query_order, usernames_from_hrefs

QUERIES = ["a", "b", "c", "d"]


def test_rotation_differs_by_slot_and_day() -> None:
    day = date(2026, 10, 16)
    morning = query_order(QUERIES, day, 0, rotate=True)
    noon = query_order(QUERIES, day, 1, rotate=True)
    tomorrow = query_order(QUERIES, day + timedelta(days=1), 0, rotate=True)

    assert sorted(morning) == QUERIES
    assert morning != noon
    assert morning != tomorrow
    assert noon == tomorrow


def test_no_rotation_keeps_order() -> None:
    assert query_order(QUERIES, date(2026, 10, 16), 2, rotate=False) == QUERIES


@pytest.mark.parametrize(
    ("hrefs", "expected"),
    [
        (["/@shop.almaty.test/live?enter_from_merge=search_result"], ["shop.almaty.test"]),
        (["/@a_b/live", "/@a_b/live?x=1", "/@c/live"], ["a_b", "c"]),
        (["/live", "/@x/video/1", None, "/@bad name/live", '/@x"]/live'], []),
        (["https://evil.example/@x/live"], []),
    ],
)
def test_usernames_from_hrefs(hrefs: list[str | None], expected: list[str]) -> None:
    assert usernames_from_hrefs(hrefs) == expected
