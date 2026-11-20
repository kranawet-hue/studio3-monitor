"""Studio3 Tracker v2: one release notification for the next calendar week.

Reads only the public Studio3 calendar. No account, booking or credential is
used. Public availability is verified in both rendered views, not inferred
from colors or an HTTP success. The old monitor.py is retained for reference.
"""
from __future__ import annotations
import argparse
import asyncio
import copy
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE = "https://www.supersaas.de/schedule/Jumping_JAG/Studio_3_Kursplan"
ZONE = ZoneInfo("Europe/Berlin")
STATE_PATH = Path("data/tracker-state.json")
PUBLIC_PATH = Path("public/latest.json")
MESSAGE = "Für nächste Woche wurden neue Termine freigeschaltet."

class VerificationError(RuntimeError):
    pass

def require(ok, message):
    if not ok:
        raise VerificationError(message)

def next_monday(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())

def in_window(day: date, today: date) -> bool:
    monday = next_monday(today)
    return monday <= day < monday + timedelta(days=7)

def view_url(view, monday):
    return BASE + "?" + urlencode(dict(view=view, day=monday.day, month=monday.month,
        year=monday.year, per_page=100))

def clean(text):
    return " ".join(str(text).split())

@dataclass(frozen=True)
class Course:
    slot_id: str
    day: str
    time: str
    title: str
    capacity: int
    used: int
    status: str = "unknown"

def read_records(snapshot):
    require(snapshot.get("rp_id") == 281170, "Unexpected Studio3 schedule ID")
    require(snapshot.get("local") is False, "Calendar timezone mode changed")
    records = snapshot.get("app")
    require(isinstance(records, list) and len(records) <= 2000, "Invalid JS calendar data")
    found = {}
    for row in records:
        require(isinstance(row, list) and len(row) >= 12, "Slot schema changed")
        require(all(type(row[i]) is int for i in (0, 1, 2, 3, 4)), "Invalid slot fields")
        require(row[0] < row[1] and row[2] > 0, "Invalid slot time or ID")
        # These are floating local calendar seconds, not UTC instants.
        # Adding the Berlin offset would shift the displayed start by 1-2 hours.
        stamp = datetime.fromtimestamp(row[0], timezone.utc)
        title, key = clean(row[7]), str(row[2])
        require(1 <= len(title) <= 200 and key not in found, "Invalid title or duplicate ID")
        found[key] = Course(key, stamp.date().isoformat(), stamp.strftime("%H:%M"), title, row[3], row[4])
    return found

def verify_common(snapshot, view, monday):
    require(snapshot.get("view") == view, "Wrong calendar view")
    require(snapshot.get("cursor", "")[:10] == monday.isoformat(), "Wrong selected date")
    soup = BeautifulSoup(snapshot.get("html", ""), "html.parser")
    require(soup.select_one("#container") is not None, "Calendar not rendered")
    return soup, read_records(snapshot)

def parse_week(snapshot, monday):
    soup, records = verify_common(snapshot, "week", monday)
    require(soup.select_one("#container.weekCont") is not None, "Week layout changed")
    visible = {}
    for offset in range(7):
        day = monday + timedelta(days=offset)
        header, cell = soup.select_one(f"#h{offset}"), soup.select_one(f"#d{offset}")
        require(header is not None and cell is not None, "Incomplete week")
        require(re.search(rf"(?<!\d){day.day}\.{day.month}\.(?!\d)", header.get_text()), "Wrong day header")
        for chip in cell.select(".chip"):
            match = re.fullmatch(r"vs\(event,(\d+)\)", chip.get("onclick", ""))
            require(match is not None, "Unknown calendar chip")
            key = match[1]
            require(key in records and key not in visible, "Chip missing from JS data or duplicated")
            course = records[key]
            require(course.day == day.isoformat(), "JS and rendered date differ")
            head = chip.select_one(".head")
            clock = re.search(r"(\d{1,2}):(\d{2})", head.get_text() if head else "")
            require(clock and f"{int(clock[1]):02}:{clock[2]}" == course.time, "JS and rendered time differ")
            require(clean(chip.get("title", "")) == course.title, "JS and rendered title differ")
            visible[key] = course
    expected = {k for k, c in records.items() if monday.isoformat() <= c.day < (monday + timedelta(days=7)).isoformat()}
    require(set(visible) == expected, "JS data and rendered week differ, including empty view")
    return visible

def parse_free(snapshot, monday):
    soup, records = verify_common(snapshot, "free", monday)
    total = snapshot.get("total")
    require(type(total) is int and 0 <= total <= 100, "Availability pagination exceeds verified limit")
    rows = soup.select('#container tr[id^="c"]')
    require(len(rows) == total == len(records), "Incomplete availability list")
    found = {}
    for row in rows:
        key = row.get("id", "")[1:]
        require(key in records and key not in found, "Unknown or duplicate availability row")
        c = records[key]
        require(row.get("onclick") == f"viewslot(event,{key})", "Availability layout changed")
        cells = row.find_all("td", recursive=False)
        require(len(cells) == 7, "Availability column schema changed")
        require(clean(cells[3].get_text()) == c.title, "Availability title mismatch")
        day, when = date.fromisoformat(c.day), clean(cells[0].get_text(" "))
        require(re.search(rf"(?<!\d){day.day}\.{day.month}\.(?!\d)", when), "Availability date mismatch")
        clock = re.search(r"(\d{1,2}):(\d{2})", when)
        require(clock and f"{int(clock[1]):02}:{clock[2]}" == c.time, "Availability time mismatch")
        classes = row.get("class", [])
        if c.capacity > 0 and c.used >= c.capacity:
            status = "full"
        elif c.capacity > 0 and c.used >= 0:
            require(clean(cells[4].get_text()) == f"{c.used} / {c.capacity}", "Capacity display mismatch")
            if "j" in classes and "g" not in classes:
                status = "bookable"
            elif "g" in classes and snapshot.get("early", 0) > 0 and row.get("title"):
                status = "not_open"
            else:
                status = "unknown"
        else:
            status = "closed"
        found[key] = Course(**(asdict(c) | {"status": status}))
    if not rows:
        require("Kein verfügbarer Termin gefunden" in soup.get_text(), "No explicit empty availability message")
    return found

def combine(week, free, today):
    require(all(k in week for k, c in free.items() if in_window(date.fromisoformat(c.day), today)),
        "New slot appeared between views; retry")
    result = {}
    for key, c in week.items():
        status = "full" if c.capacity > 0 and c.used >= c.capacity else "unknown"
        if key in free:
            f = free[key]
            require((c.day, c.time, c.title, c.capacity) == (f.day, f.time, f.title, f.capacity),
                "Calendar changed between views; retry next run")
            if status != "full":
                status = f.status
        result[key] = Course(**(asdict(c) | {"status": status}))
    return result

async def capture(page, view, monday):
    response = await page.goto(view_url(view, monday), wait_until="networkidle", timeout=45000)
    require(response is not None and response.status == 200, "Calendar HTTP error")
    url = urlparse(page.url)
    require(url.scheme == "https" and url.netloc == "www.supersaas.de" and url.path == urlparse(BASE).path,
        "Unexpected redirect; only Studio3 is allowed")
    await page.locator("#container").wait_for(state="visible", timeout=15000)
    return await page.evaluate("""() => ({view, cursor:cursor.toISOString(), local, rp_id, app,
      total:typeof total_entries==='undefined'?null:total_entries,
      early:typeof early_limit==='undefined'?null:early_limit,
      html:document.querySelector('#container').outerHTML})""")

async def read_calendar(today, *, include_current=False):
    monday = next_monday(today)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path=os.getenv("CHROMIUM_EXECUTABLE") or None)
        page = await browser.new_page(locale="de-DE", timezone_id="Europe/Berlin", viewport={"width": 1440, "height": 1000})
        try:
            week = parse_week(await capture(page, "week", monday), monday)
            free = parse_free(await capture(page, "free", monday), monday)
            upcoming = combine(week, free, today)
            if not include_current:
                return upcoming
            current_monday = monday - timedelta(days=7)
            current = parse_week(await capture(page, "week", current_monday), current_monday)
            # Past/current-week slots are for display only and never enter alert history.
            return upcoming, current
        except Exception:
            Path("debug").mkdir(exist_ok=True)
            try:
                await page.screenshot(path="debug/failure.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            await browser.close()

def empty_state():
    return {"version": 2, "weeks": {}, "events": {}, "last_success": None}

def load_state(path=STATE_PATH):
    if not path.exists():
        return empty_state()
    state = json.loads(path.read_text(encoding="utf8"))
    require(state.get("version") == 2, "Unknown state version: refusing history reset")
    require(isinstance(state.get("weeks"), dict) and isinstance(state.get("events"), dict), "Invalid persisted state")
    for week, info in state["weeks"].items():
        require(date.fromisoformat(week).weekday() == 0 and isinstance(info.get("known"), dict), "Corrupt week state")
        require(type(info.get("notified")) is bool, "Corrupt delivery state")
    return state

def calculate_updates(state, courses, now):
    """Silent first baseline per week. Stable IDs; full slots never trigger.

    Two available observations >=60s apart confirm new publication or a known
    explicitly not-yet-open slot becoming available. One release notice/week.
    """
    require(now.tzinfo is not None, "Timezone-aware clock required")
    now = now.astimezone(ZONE)
    monday = next_monday(now.date()).isoformat()
    require(all(in_window(date.fromisoformat(c.day), now.date()) for c in courses.values()), "Out-of-window courses")
    state = copy.deepcopy(state)
    require(state.get("version") == 2, "Wrong state version")
    weeks = state["weeks"]
    baseline = monday not in weeks
    week = weeks.setdefault(monday, {"known": {}, "notified": False})
    known, nowstr = week["known"], now.isoformat()
    previous = state.get("last_success")
    gap = (now - datetime.fromisoformat(previous)).total_seconds() if previous else None
    require(gap is None or gap >= 0, "Clock moved backwards")
    eligible = False
    for key, c in courses.items():
        old = known.get(key)
        if old is None:
            candidate = c.status == "not_open" or (not baseline and c.status == "bookable")
            old = known[key] = {"candidate": candidate, "candidate_checked": True, "ever_full": False,
                "streak": 0, "confirmed_at": None}
        elif not old.get("candidate_checked"):
            if old.get("course", {}).get("status") in {"unknown", "closed"}:
                old["candidate"] = False
            old["candidate_checked"] = True
        old["ever_full"] |= c.status == "full"
        if c.status == "bookable" and old["candidate"] and not old["ever_full"]:
            since = (now - datetime.fromisoformat(old["confirmed_at"])).total_seconds() if old["confirmed_at"] else None
            if since is None or since > 90 * 60:
                old["streak"] = 1
                old["confirmed_at"] = nowstr
            elif since >= 60:
                old["streak"] += 1
                old["confirmed_at"] = nowstr
            eligible |= old["streak"] >= 2
        else:
            old["streak"], old["confirmed_at"] = 0, None
        old["course"] = asdict(c)
    for key in set(known) - set(courses):
        known[key]["streak"], known[key]["confirmed_at"] = 0, None
    new = None
    if eligible and not week["notified"]:
        new = {"id": f"release:{monday}", "week_start": monday,
            "detected_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        state["events"][new["id"]] = new
        week["notified"] = True
    state["last_success"] = nowstr
    cutoff = (now.date() - timedelta(days=35)).isoformat()
    state["weeks"] = {k: v for k, v in weeks.items() if k >= cutoff}
    state["events"] = {k: v for k, v in state["events"].items() if v["week_start"] >= cutoff}
    return state, new

def build_feed(state, courses, now, current_courses=None):
    return {"version": 2, "status": "verified", "source": BASE,
        "verified_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "week_start": next_monday(now.astimezone(ZONE).date()).isoformat(),
        "available_count": sum(c.status == "bookable" for c in courses.values()),
        "courses": [asdict(c) for c in sorted(courses.values(), key=lambda c: (c.day, c.time, c.slot_id))],
        "current_week": {
            "week_start": (next_monday(now.astimezone(ZONE).date()) - timedelta(days=7)).isoformat(),
            "courses": [asdict(c) for c in sorted((current_courses or {}).values(), key=lambda c: (c.day, c.time, c.slot_id))],
        } if current_courses is not None else None,
        "events": sorted(state["events"].values(), key=lambda e: e["detected_at"])}

def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    temp.replace(path)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No state or feed writes")
    args = parser.parse_args()
    now = datetime.now(ZONE)
    try:
        state = load_state()
        courses, current_courses = await read_calendar(now.date(), include_current=True)
        require(next_monday(datetime.now(ZONE).date()) == next_monday(now.date()), "Week changed while reading")
        updated, notice = calculate_updates(state, courses, now)
        print(json.dumps({"week_start": next_monday(now.date()).isoformat(), "slots": len(courses),
            "current_week_slots": len(current_courses),
            "publicly_available": sum(c.status == "bookable" for c in courses.values()),
            "new_release": notice is not None, "dry_run": args.dry_run}, ensure_ascii=False))
        if not args.dry_run:
            # Persistent outbox first; CI publishes both files in one commit.
            atomic_write(STATE_PATH, updated)
            atomic_write(PUBLIC_PATH, build_feed(updated, courses, now, current_courses))
        return 0
    except Exception as exc:
        print(f"NOT VERIFIED: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
