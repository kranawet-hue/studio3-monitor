from datetime import datetime
from zoneinfo import ZoneInfo

from monitor import BASE, Course, build_feed, calculate_updates, course_for_feed


def test_feed_is_for_studio3_only():
    now = datetime(2026, 11, 15, 12, tzinfo=ZoneInfo("Europe/Berlin"))
    c = Course("2026-11-18", "18:30", "Jumping Fitness", BASE)
    content = build_feed({"events": {}}, {c.key: c}, now)
    assert content["status"] == "verified"
    assert content["source"] == BASE
    assert content["verified_at"] == "2026-11-15T11:00:00Z"
    assert content["courses"] == [course_for_feed(c)]
    assert content["events"] == []


def test_only_future_weeks_in_feed():
    now = datetime(2026, 11, 15, 12, tzinfo=ZoneInfo("Europe/Berlin"))
    earlier = Course("2026-11-05", "18:30", "Jumping Fitness", BASE)
    future = Course("2026-11-20", "19:30", "Jumping Fitness", BASE)
    out = build_feed({}, {earlier.key: earlier, future.key: future}, now)
    assert [e["day"] for e in out["courses"]] == ["2026-11-20"]


def test_new_vs_reopen_capacity():
    a = Course("2026-11-18", "18:30", "Jumping Fitness", BASE)
    b = Course("2026-11-19", "19:30", "Jumping Fitness", BASE)
    now = datetime(2026, 11, 15, 12, tzinfo=ZoneInfo("Europe/Berlin"))
    state = {"version": 1, "bootstrapped": False, "known": {}, "pending": {}, "sent": {}}
    state, events = calculate_updates(state, {a.key: a}, {}, now)
    assert not events
    state, events = calculate_updates(state, {a.key: a}, {a.key: a}, now)
    assert not events  # place reopened, but course existed
    state, events = calculate_updates(state, {a.key: a, b.key: b}, {a.key: a, b.key: b}, now)
    assert not events
    state, events = calculate_updates(state, {a.key: a, b.key: b}, {a.key: a, b.key: b}, now)
    assert [x.key for x in events] == [b.key]


def test_title_change_same_slot_does_not_become_new_course():
    a = Course("2026-11-18", "18:30", "Jumping Fitness", BASE, "1234")
    b = Course("2026-11-18", "18:30", "Jumping Fitness XXL", BASE, "1234")
    assert a.key == b.key


def test_new_slot_at_same_time_is_new_course():
    a = Course("2026-11-18", "18:30", "Jumping Fitness", BASE, "1234")
    b = Course("2026-11-18", "18:30", "Jumping Fitness", BASE, "5678")
    assert a.key != b.key
