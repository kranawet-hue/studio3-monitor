from datetime import date, datetime
from zoneinfo import ZoneInfo

from monitor import Course, calculate_updates, extract_courses, in_window, parse_day, week_starts


HTML = '''<html><body>
<div class="slot" data-slot-id="4251" data-start="2026-11-18 18:30:00" data-title="Jumping Fitness">
<a href="/schedule/Jumping_JAG/Studio_3_Kursplan?slot=4251">Buchen</a></div>
<div class="slot" data-slot-id="4252" data-start="2026-11-19 19:00:00" data-title="Cross Jump">
<a href="/schedule/Jumping_JAG/Studio_3_Kursplan?slot=4252">Buchen</a></div>
</body></html>'''


def test_extract_and_clock():
    found = extract_courses(HTML, origin="https://www.supersaas.de/schedule/Jumping_JAG/Studio_3_Kursplan", reference_year=2026)
    assert len(found) == 2
    a = [x for x in found.values() if x.slot_id == "4251"][0]
    assert (a.day, a.time, a.title) == ("2026-11-18", "18:30", "Jumping Fitness")


def test_dates():
    assert parse_day("Mittwoch 18. November 2026", 2026) == date(2026, 11, 18)
    assert parse_day("18.11.2026", 2026) == date(2026, 11, 18)
    assert parse_day("2026-11-18", 2026) == date(2026, 11, 18)
    assert week_starts(date(2026, 11, 15)) == [date(2026, 11, 9), date(2026, 11, 16)]


def test_new_entries_only_and_no_free_place_false_positive():
    now = datetime(2026, 11, 15, 9, tzinfo=ZoneInfo("Europe/Berlin"))
    a = Course("2026-11-18", "18:30", "Jumping Fitness", "https://example.com/slot1")
    b = Course("2026-11-19", "19:00", "Cross Jump", "https://example.com/slot2")
    s = {"version": 1, "bootstrapped": False, "known": {}, "pending": {}, "sent": {}}
    s, a1 = calculate_updates(s, {a.key: a}, {}, now)
    assert not a1
    # It was initially full, then a place opened: never call this a new class.
    s, a2 = calculate_updates(s, {a.key: a}, {a.key: a}, now)
    assert not a2
    # Newly created class, present in both views, needs one verification repeat.
    s, a3 = calculate_updates(s, {a.key: a, b.key: b}, {a.key: a, b.key: b}, now)
    assert not a3
    s, a4 = calculate_updates(s, {a.key: a, b.key: b}, {a.key: a, b.key: b}, now)
    assert [x.key for x in a4] == [b.key]
    s['sent'][b.key] = now.isoformat()
    s, a5 = calculate_updates(s, {a.key: a, b.key: b}, {a.key: a, b.key: b}, now)
    assert not a5


def test_week_window():
    assert in_window(Course("2026-11-18", "18:30", "Jumping Fitness", "https://example.com"), date(2026, 11, 15))
    assert not in_window(Course("2026-11-23", "18:30", "Jumping Fitness", "https://example.com"), date(2026, 11, 15))


def test_empty_week_bootstrap_then_new_course_alert():
    now = datetime(2026, 11, 15, 10, tzinfo=ZoneInfo("Europe/Berlin"))
    s = {"version": 1, "bootstrapped": False, "known": {}, "pending": {}, "sent": {}}
    s, alerts = calculate_updates(s, {}, {}, now)
    assert not alerts and s["bootstrapped"]
    new = Course("2026-11-18", "18:30", "Jumping Fitness", "https://example.com")
    s, alerts = calculate_updates(s, {new.key: new}, {new.key: new}, now)
    assert not alerts
    s, alerts = calculate_updates(s, {new.key: new}, {new.key: new}, now)
    assert [x.key for x in alerts] == [new.key]
