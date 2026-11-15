#!/usr/bin/env python3
"""Conservative SuperSaaS class monitor using hosted Chromium + Telegram.

The scraper intentionally fails closed: a class must be visible in the week
calendar and in the available-slots list before a notification can be sent.
An unreadable page never becomes an empty schedule.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright

BASE = "https://www.supersaas.de/schedule/Jumping_JAG/Studio_3_Kursplan"
PUBLIC_BASES = (
    BASE,
    "https://www.supersaas.com/schedule/Jumping_JAG/Studio_3_Kursplan",
    "https://www.supersaas.net/schedule/Jumping_JAG/Studio_3_Kursplan",
)
STATE_PATH = Path("data/state.json")
PUBLIC_PATH = Path("public/latest.json")
ZONE = ZoneInfo("Europe/Berlin")
HEADLESS = os.getenv("HEADLESS", "1") != "0"
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
TIMEOUT_MS = 30000

MONTHS = {"januar": 1, "februar": 2, "marz": 3, "maerz": 3, "april": 4,
          "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
          "oktober": 10, "november": 11, "dezember": 12}
FULL_DATE = re.compile(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})(?!\d)")
ISO_DATE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
GER_DATE = re.compile(r"(?<!\d)(\d{1,2})\.?(?:\s+)(Januar|Februar|M(?:ä|ae)rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)(?:\s+|\s*,\s*)(20\d{2})", re.I)
GER_DATE_NO_YEAR = re.compile(r"(?<!\d)(\d{1,2})\.?(?:\s+)(Januar|Februar|M(?:ä|ae)rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\b", re.I)
TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
SLOT_QUERY_KEYS = ("slot_id", "slot", "slotid")
BLOCK_WORDS = ("ausgebucht", "nicht verfügbar", "nicht verfugbar", "cancelled", "storniert", "geschlossen")
GENERIC = {"buchen", "reservieren", "book", "book now", "jetzt buchen", "frei", "verfugbar", "verfügbar", "details", "mehr", "weiter", "select"}


@dataclass(frozen=True)
class Course:
    day: str
    time: str
    title: str
    url: str
    slot_id: str = ""

    @property
    def key(self) -> str:
        # Name, date and time, not remaining capacity, define a course.
        # A renamed class with the same stable slot ID is not a new class.
        # If SuperSaaS does not expose an ID, use normalized title as fallback.
        label = f"slot:{self.slot_id}" if self.slot_id else re.sub(r"\s+", " ", self.title.casefold()).strip()
        return f"{self.day}|{self.time}|{label}"


def clean(s: str) -> str:
    return " ".join(str(s).split())


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def parse_day(s: str, default_year: int) -> date | None:
    if not s:
        return None
    m = ISO_DATE.search(s)
    if m:
        try:
            return date(*map(int, m.groups()))
        except ValueError:
            return None
    m = FULL_DATE.search(s)
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    m = GER_DATE.search(s)
    if m:
        try:
            return date(int(m[3]), MONTHS[norm(m[2])], int(m[1]))
        except (KeyError, ValueError):
            return None
    m = GER_DATE_NO_YEAR.search(s)
    if m:
        try:
            return date(default_year, MONTHS[norm(m[2])], int(m[1]))
        except (KeyError, ValueError):
            return None
    # Dates with no year are accepted only with an explicit caller year.
    m = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(?!\d)", s)
    if m:
        try:
            return date(default_year, int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def parse_clock(s: str) -> str | None:
    m = TIME.search(s or "")
    return f"{int(m[1]):02d}:{int(m[2]):02d}" if m else None


def meaningful_title(s: str) -> str | None:
    s = clean(s)
    s = ISO_DATE.sub(" ", FULL_DATE.sub(" ", s))
    s = GER_DATE.sub(" ", s)
    s = TIME.sub(" ", s)
    s = re.sub(r"\b(?:Plätze?|places?)\s*:?\s*\d+\b", "", s, flags=re.I)
    s = clean(s.strip("-–|,:; "))
    if not s or len(s) > 150 or len(s) < 4 or norm(s) in {norm(x) for x in GENERIC}:
        return None
    if re.fullmatch(r"[\d\s/\-.,]+", s):
        return None
    return s


def extract_href_slot_id(href: str) -> str:
    params = parse_qs(urlparse(href).query)
    for key in SLOT_QUERY_KEYS:
        if key in params and params[key][0].isdigit():
            return params[key][0]
    m = re.search(r"/(?:slots|slot)/(\d+)(?:[/?#]|$)", href)
    return m[1] if m else ""


def attr_line(tag: Tag) -> str:
    names = ("datetime", "data-start", "data-start-time", "data-starttime", "data-datetime", "data-date", "data-day", "data-time", "data-title", "title", "aria-label", "data-slot-id")
    return " ".join(clean(tag.get(n, "")) for n in names if tag.has_attr(n))


def candidate_tags(soup: BeautifulSoup) -> list[Tag]:
    selectors = "[data-slot-id], [data-start], [data-start-time], [data-date][data-title], .slot, .slot-item, .timeslot, .appointment, [class*='slot'], a[href*='slot='], a[href*='slot_id='], a[href*='/slots/']"
    return list(soup.select(selectors))


def extract_courses(html: str, *, origin: str, reference_year: int, day_hint: date | None = None) -> dict[str, Course]:
    """Extract only dated, timed, titled *slot-like* elements; never infer courses from cell colours."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Course] = {}
    for tag in candidate_tags(soup):
        if tag.name in ("script", "style"):
            continue
        classes = " ".join(tag.get("class", []))
        if any(w in norm(classes) for w in ("week-nav", "mini-calendar", "calendar-nav")):
            continue
        # Scope: current element first, then at most two ancestors for date/time/title.
        contexts: list[Tag] = [tag]
        p = tag.parent
        for _ in range(2):
            if isinstance(p, Tag) and p.name not in ("html", "body"):
                contexts.append(p)
                p = p.parent
            else:
                break
        raw = clean(" ".join(attr_line(x) + " " + x.get_text(" ", strip=True) for x in contexts[:2]))
        if any(w in norm(raw) for w in BLOCK_WORDS):
            # Full courses must be picked up as known existing in the week view.
            # The 'bookable' judgment comes only from the separate free view.
            pass
        day = parse_day(raw, reference_year)
        if not day:
            a = tag if tag.name == "a" else tag.find("a", href=True)
            if a and a.get("href"):
                q = parse_qs(urlparse(a["href"]).query)
                if all(k in q for k in ("day", "month", "year")):
                    try:
                        day = date(int(q["year"][0]), int(q["month"][0]), int(q["day"][0]))
                    except (ValueError, IndexError):
                        pass
        if not day:
            # Month/week day headers are often parent elements.
            for ctx in contexts[2:]:
                day = parse_day(attr_line(ctx) + " " + ctx.get_text(" ", strip=True)[:250], reference_year)
                if day:
                    break
        if day is None and day_hint is not None:
            day = day_hint
        at = parse_clock(raw)
        if not day or not at:
            continue
        link = tag if tag.name == "a" else tag.find("a", href=True)
        href = link.get("href", "") if link else ""
        slot_id = clean(tag.get("data-slot-id", ""))
        if not slot_id and href:
            slot_id = extract_href_slot_id(href)
        title = meaningful_title(clean(tag.get("data-title", "") or tag.get("title", "") or tag.get("aria-label", "")))
        if not title:
            title_el = tag.select_one(".slot-title, .title, .appointment-title, strong, b")
            if title_el:
                title = meaningful_title(title_el.get_text(" ", strip=True))
        if not title:
            title = meaningful_title(tag.get_text(" ", strip=True))
        if not title and link:
            title = meaningful_title(link.get_text(" ", strip=True))
        if not title:
            continue
        c = Course(day.isoformat(), at, title, urljoin(origin, href) if href else BASE, slot_id)
        found[c.key] = c
    return found


def week_starts(today: date) -> list[date]:
    current_monday = today - timedelta(days=today.weekday())
    return [current_monday, current_monday + timedelta(days=7)]


def in_window(course: Course, today: date) -> bool:
    starts = week_starts(today)
    d = date.fromisoformat(course.day)
    return today <= d < starts[1] + timedelta(days=7)


def calendar_shell(html: str, text: str) -> bool:
    """A plausible rendered calendar, even when no courses were published yet."""
    soup = BeautifulSoup(html, "html.parser")
    body = norm(text)
    calendar_structure = soup.select_one(
        "#calendar, .calendar, #schedule, .schedule, "
        "[class*='calendar'], [class*='timetable'], [id*='calendar'], "
        "a[href*='view=week'], a[href*='view=month']"
    ) is not None
    navigation_text = ("monat" in body and "woche" in body) or ("month" in body and "week" in body)
    return (calendar_structure or navigation_text) and len(body) >= 35


def view_url(view: str, day: date, per_page: int = 100, base: str = BASE) -> str:
    query = urlencode({"view": view, "day": day.day, "month": day.month, "year": day.year, "per_page": per_page})
    return base + "?" + query


async def render(page, url: str) -> tuple[str, str, str]:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(1800)
    if not response or response.status >= 400:
        raise RuntimeError(f"HTTP {response.status if response else 'no response'} for {url}")
    if "supersaas" not in urlparse(page.url).netloc.casefold():
        raise RuntimeError(f"Unexpected redirect domain {urlparse(page.url).netloc}")
    html = await page.content()
    text = (await page.locator("body").inner_text(timeout=5000))[:3500]
    if any(x in norm(text) for x in ("anmelden, um", "login required", "access denied", "nicht gefunden", "not found")):
        raise RuntimeError("Schedule cannot be read publicly or needs login")
    return html, text, page.url


async def read_calendar(today: date) -> tuple[dict[str, Course], dict[str, Course], list[str]]:
    """Try official public domains; publish only if all views are readable."""
    attempts: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        try:
            for base in PUBLIC_BASES:
                page = await browser.new_page(locale="de-DE", timezone_id="Europe/Berlin",
                                              viewport={"width": 1440, "height": 1000})
                seen_all: dict[str, Course] = {}
                seen_free: dict[str, Course] = {}
                debug: list[str] = []
                try:
                    for monday in week_starts(today):
                        url = view_url("week", monday, base=base)
                        html, txt, landed = await render(page, url)
                        if "view=week" not in landed:
                            raise RuntimeError("Week view unavailable or redirected")
                        if not calendar_shell(html, txt):
                            raise RuntimeError(f"Week view has no verifiable calendar structure: {landed}")
                        found = extract_courses(html, origin=landed, reference_year=monday.year)
                        seen_all.update({k: v for k, v in found.items() if in_window(v, today)})
                        debug.append(f"Week {monday}: {len(found)} slot-like records, landed={landed}")
                    url = view_url("free", today, base=base)
                    html, txt, landed = await render(page, url)
                    if "view=free" not in landed:
                        raise RuntimeError("Free view unavailable or redirected")
                    if not calendar_shell(html, txt):
                        raise RuntimeError(f"Free view has no verifiable calendar structure: {landed}")
                    found = extract_courses(html, origin=landed, reference_year=today.year)
                    seen_free.update({k: v for k, v in found.items() if in_window(v, today)})
                    debug.append(f"Free: {len(found)} slot-like records, landed={landed}")
                    unknown = set(seen_free) - set(seen_all)
                    if unknown:
                        debug.append(f"Unmatched free entries ({len(unknown)}) ignored")
                        seen_free = {k: c for k, c in seen_free.items() if k in seen_all}
                    debug.append(f"Verified public domain: {base}")
                    return seen_all, seen_free, debug
                except Exception as exc:
                    attempts.append(f"{base}: {exc}")
                    try:
                        Path("debug").mkdir(exist_ok=True)
                        await page.screenshot(path="debug/failure.png", full_page=True, timeout=8000)
                    except Exception:
                        pass
                finally:
                    await page.close()
        finally:
            await browser.close()
    raise RuntimeError("No verifiable public Studio 3 calendar: " + " | ".join(attempts))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "bootstrapped": False, "known": {}, "pending": {}, "sent": {}}
    s = json.loads(STATE_PATH.read_text(encoding="utf8"))
    if s.get("version") != 1:
        raise RuntimeError("Unknown state version; refusing to reset history")
    return s


def save_state(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf8")
    tmp.replace(STATE_PATH)


def calculate_updates(state: dict, all_courses: dict[str, Course], free_courses: dict[str, Course], now: datetime) -> tuple[dict, list[Course]]:
    """New course needs 2 successive valid observations and visibility in both lists.

    State holds all known courses (including fully booked), not just free ones.
    """
    state = json.loads(json.dumps(state))  # copy
    known = state.setdefault("known", {})
    pending = state.setdefault("pending", {})
    sent = state.setdefault("sent", {})
    nowstr = now.isoformat(timespec="seconds")
    if not state.get("bootstrapped"):
        known.update({k: asdict(v) for k, v in all_courses.items()})
        state["bootstrapped"] = True
        return state, []

    fresh = set(all_courses) - set(known)
    # Wait until the newly published item appears in two successful checks.
    for key in fresh:
        if key in pending:
            pending[key]["count"] += 1
        else:
            pending[key] = {"count": 1, "first_seen": nowstr}

    for key in list(pending):
        if key not in fresh:
            pending.pop(key, None)
    alerts: list[Course] = []
    for key in fresh:
        if pending[key]["count"] >= 2 and key in free_courses and key not in sent:
            alerts.append(free_courses[key])
    # Newly added entries that are NOT bookable yet remain pending, waiting for availability.
    # The known baseline is advanced for all *existing* events, except fresh ones until
    # their second successful sighting; then mark them as known even if full.
    for key, obj in all_courses.items():
        if key in fresh and pending.get(key, {}).get("count", 0) < 2:
            continue
        known[key] = asdict(obj)
        if key in fresh and key not in free_courses:
            pending.pop(key, None)  # first seen fully booked -> never announce a later opening
    # Trim ancient known entries and delivery keys after 35 days.
    cutoff = (now.date() - timedelta(days=35)).isoformat()
    for store in (known, sent):
        for key in list(store):
            if key.split("|", 1)[0] < cutoff:
                store.pop(key, None)
    return state, sorted(alerts, key=lambda x: (x.day, x.time, x.title))




def course_for_feed(course: Course) -> dict:
    return {
        "id": course.key,
        "day": course.day,
        "time": course.time,
        "title": course.title,
        "url": course.url,
    }


def build_feed(state: dict, free_courses: dict[str, Course], now: datetime) -> dict:
    # DO NOT publish a fabricated empty feed. Call this only after successful
    # rendered-page checks, and only from the validated read_calendar() path.
    valid = [c for k, c in free_courses.items() if in_window(c, now.date())]
    events = list(state.get("events", {}).values())
    return {
        "version": 1,
        "status": "verified",
        "source": BASE,
        "verified_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "courses": [course_for_feed(c) for c in sorted(valid, key=lambda x: (x.day, x.time, x.title))],
        "events": sorted(events, key=lambda e: e["detected_at"]),
    }


def publish_feed(state: dict, free_courses: dict[str, Course], now: datetime) -> None:
    snapshot = build_feed(state, free_courses, now)
    PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = PUBLIC_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(PUBLIC_PATH)


def telegram_send(courses: list[Course]) -> None:
    if not courses:
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    lines = ["🆕 Neue Kurse im Studio 3:"]
    for c in courses:
        d = date.fromisoformat(c.day).strftime("%d.%m.%Y")
        lines += [f"• {d}, {c.time} Uhr — {c.title}"]
    lines += ["", "Kursplan:", BASE]
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         data={"chat_id": chat_id, "text": "\n".join(lines), "disable_web_page_preview": "true"}, timeout=20)
    resp.raise_for_status()
    if not resp.json().get("ok"):
        raise RuntimeError("Telegram API did not confirm delivery")


async def main() -> int:
    now = datetime.now(ZONE)
    today = now.date()
    state = load_state()
    try:
        all_courses, free_courses, debug = await read_calendar(today)
    except Exception as exc:
        print(f"NOT VERIFIED — no state change and no alert: {exc}", file=sys.stderr)
        return 2
    for line in debug:
        print(line)
    updated, new = calculate_updates(state, all_courses, free_courses, now)
    print(f"Verified pages: all={len(all_courses)}, bookable={len(free_courses)}, new={len(new)}")
    for c in new:
        print("NEW", c.day, c.time, c.title)
    if DRY_RUN:
        print("DRY_RUN=1, no notification and no state persisted")
        return 0
    # Telegram is optional. The Android app receives all deduplicated events
    # through the HTTPS JSON feed; no bot token is required for mobile alerts.
    if new and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        telegram_send(new)
    for c in new:
        updated["sent"][c.key] = now.isoformat(timespec="seconds")
        updated.setdefault("events", {})[c.key] = {
            "id": c.key,
            "detected_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "course": course_for_feed(c),
        }
    # Keep events long enough for a temporarily offline phone to catch up.
    cutoff = (today - timedelta(days=3)).isoformat()
    for key in list(updated.setdefault("events", {})):
        if key.split("|", 1)[0] < cutoff:
            updated["events"].pop(key, None)
    save_state(updated)
    publish_feed(updated, free_courses, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
