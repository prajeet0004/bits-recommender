"""Timetable intelligence (the task's brownie point), all plain code.

1. Build the student's busy slots from the courses they're taking now.
   A course with one section of a type is known; with several, the student
   picks their section in the profile, otherwise we can't know and say so.
2. For a candidate course, pick a section of each type (lecture, tutorial,
   practical) that doesn't clash and respects "no 8 AM" / "keep <day> free".
   If L1 clashes but L2 is free, L2 is suggested instead of rejecting the course.
3. Midsem / compre clashes: same date and session as a current course.

BITS hours: hour 1 = 8 AM, hour 2 = 9 AM, ... hour 10 = 5 PM.
"""
from collections import defaultdict

TYPES = ("lecture", "tutorial", "practical")
DAY_ORDER = ["M", "T", "W", "Th", "F", "S", "Su"]
DAY_NAMES = {"monday": "M", "tuesday": "T", "wednesday": "W", "thursday": "Th",
             "friday": "F", "saturday": "S", "sunday": "Su"}


def meetings(section):
    s = section.get("slots")
    return {(d, h) for d, h in s["meetings"]} if s and s.get("meetings") else set()


def options(tt_course):
    """type -> list of non-cancelled sections"""
    out = defaultdict(list)
    for s in tt_course["sections"]:
        if not s["cancelled"]:
            out[s["type"]].append(s)
    return out


def describe(section):
    """[['M',3],['W',3],['Th',9]] -> 'M W 3, Th 9'"""
    by_hours = defaultdict(list)
    for d, h in sorted(meetings(section), key=lambda m: (DAY_ORDER.index(m[0]), m[1])):
        by_hours[d].append(h)
    groups = defaultdict(list)                       # same hours -> group the days
    for d, hs in by_hours.items():
        groups[tuple(hs)].append(d)
    parts = [f"{' '.join(ds)} {','.join(map(str, hs))}" for hs, ds in groups.items()]
    return f"{section['section']}: " + ("; ".join(parts) if parts else "no fixed slot")


def busy_schedule(student, timetable_by_code):
    """Return (busy {(day,hour): code}, exams {(kind, slot): code}, unknown [text])."""
    busy, exams, unknown = {}, {}, []
    chosen = student.get("sections") or {}
    for code in student.get("current", []):
        tt = timetable_by_code.get(code)
        if not tt:
            continue
        for kind in ("midsem", "compre"):
            if tt.get(kind):
                exams[(kind, tt[kind])] = code
        for typ, secs in options(tt).items():
            pick = chosen.get(code, {}).get(typ)
            sec = next((s for s in secs if s["section"] == pick), None)
            if sec is None and len(secs) == 1:
                sec = secs[0]
            if sec is None:
                unknown.append(f"{code} {typ}")
                continue
            for m in meetings(sec):
                busy[m] = code
    return busy, exams, unknown


def allowed(section, no_8am=False, free_days=()):
    ms = meetings(section)
    if no_8am and any(h == 1 for _, h in ms):
        return False
    return not any(d in free_days for d, _ in ms)


def gaps(slots):
    """Idle hours between classes, summed over days."""
    total = 0
    by_day = defaultdict(set)
    for d, h in slots:
        by_day[d].add(h)
    for hs in by_day.values():
        total += (max(hs) - min(hs) + 1) - len(hs)
    return total


def plan(tt_course, busy, exams, no_8am=False, free_days=(), compact=False):
    """Pick one section per type. Returns (plan dict or None, reason text)."""
    for kind in ("midsem", "compre"):
        slot = tt_course.get(kind)
        if slot and (kind, slot) in exams:
            return None, f"{kind} ({slot}) clashes with {exams[(kind, slot)]}"

    picked, used = {}, set(busy)
    for typ in TYPES:
        secs = options(tt_course).get(typ, [])
        if not secs:
            continue
        free = [s for s in secs if not (meetings(s) & used)]
        if not free:
            who = sorted({busy[m] for s in secs for m in meetings(s) & set(busy)})
            return None, f"every {typ} section clashes with {', '.join(who) or 'your timetable'}"
        ok = [s for s in free if allowed(s, no_8am, free_days)]
        if not ok:
            return None, f"no {typ} section fits your time preferences"
        if compact:
            ok.sort(key=lambda s: gaps(used | meetings(s)))
        picked[typ] = ok[0]
        used |= meetings(ok[0])

    added_gaps = gaps(used) - gaps(set(busy))
    return {"sections": {t: s["section"] for t, s in picked.items()},
            "schedule": [describe(s) for s in picked.values()],
            "added_gaps": added_gaps}, ""


def parse_free_days(words):
    """['Friday', 'sat'] -> {'F', 'S'}"""
    out = set()
    for w in words or []:
        w = w.strip().lower()
        for name, code in DAY_NAMES.items():
            if name.startswith(w[:3]) and len(w) >= 3:
                out.add(code)
    return out
