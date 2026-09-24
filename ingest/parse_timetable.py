"""Parse the BITS coursewise timetable PDF into structured JSON.

Approach: pdfplumber gives every word with its x/y position. We find the
column boundaries from the header on each page, group words into lines by
their y position, then walk lines top-to-bottom:
  - a number in the COM COD column  -> a new course starts
  - a code like L1 / T2 / P3 in SEC  -> a new section starts
  - anything else                    -> continuation (extra instructors)
"""
import json, re, sys
from collections import defaultdict
import pdfplumber

SEC_RE = re.compile(r"^[LTP]\d+$")
CODE_RE = re.compile(r"^(\d+)\s+([A-Z]{2,5})\s+([A-Z]\d{3}[A-Z]?(?:-\d)?)\s*(.*)$")
DAY_TOKENS = {"M", "T", "W", "Th", "F", "S", "Su"}


def column_edges(words):
    """Find x-positions of header words on this page. Returns None if no header."""
    pos = {}
    for w in words:
        t = w["text"]
        if t == "SEC" and "sec" not in pos: pos["sec"] = w["x0"]
        elif t == "ROOM": pos["room"] = w["x0"]
        elif t == "DAYS": pos["days"] = w["x0"]
        elif t == "MIDSEM": pos["midsem"] = w["x0"]
        elif t == "COMPRE": pos["compre"] = w["x0"]
        elif t == "TITLE": pos["title"] = w["x0"] - 40  # 'COURSE TITLE' starts earlier
        elif t == "INSTRUCTOR-IN-CHARGE": pos["instr"] = w["x0"]
        elif t == "L" and "L" not in pos: pos["L"] = w["x0"]
    need = {"sec", "room", "days", "midsem", "compre", "instr", "L"}
    return pos if need <= pos.keys() else None


def bucket(x, e):
    """Which column a word at x belongs to."""
    if x < e["title"] - 50: return "comcode"
    if x < e["title"]: return "course_no"
    if x < e["L"] - 5: return "title"
    if x < e["sec"] - 3: return "credits"
    if x < e["instr"] - 3: return "sec"
    if x < e["room"] - 3: return "instr"
    if x < e["days"] - 12: return "room"
    if x < e["midsem"] - 3: return "days"
    if x < e["compre"] - 3: return "midsem"
    return "compre"


def lines_of(words, header_bottom, tol=2.5):
    """Group words into text lines. Words whose tops are within `tol` points
    of the line's first word are on the same line (rounding to a grid breaks
    lines that straddle a grid boundary)."""
    lines = []
    for w in sorted((w for w in words if w["top"] > header_bottom), key=lambda w: w["top"]):
        if lines and w["top"] - lines[-1][0]["top"] <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in lines]


def parse_days_hours(tokens):
    """['M','W','F','10'] -> {'days':['M','W','F'], 'hours':[10]}"""
    days = [t for t in tokens if t in DAY_TOKENS]
    hours = [int(t) for t in tokens if t.isdigit()]
    # 'MW' glued together sometimes
    for t in tokens:
        if t not in DAY_TOKENS and not t.isdigit():
            days += re.findall(r"Th|Su|M|T|W|F|S", t)
    return {"days": days, "hours": hours} if days else None


def parse(path):
    courses, cur, sec, edges, active = [], None, None, None, False
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words()
            e = column_edges(words)
            if e: edges = e
            texts = {w["text"] for w in words}
            if "COURSEWISE" in texts: active = True          # section II starts
            if "III.TEXT" in texts or "BOOKS" in texts: break   # section III: stop
            if not active or not edges: continue
            # skip the header block if this page has one
            hb = max((w["bottom"] for w in words if w["text"] in ("H", "SESSION", "COD")), default=0)
            for line in lines_of(words, hb):
                if line[0]["text"].startswith("Note"): continue
                cols = defaultdict(list)
                for w in line:
                    cols[bucket(w["x0"], edges)].append(w["text"])
                cc = cols.get("comcode", [])
                if cc and cc[0].isdigit():                       # new course
                    left = " ".join(cc + cols.get("course_no", []) + cols.get("title", []))
                    m = CODE_RE.match(left)
                    if not m:
                        print(f"  ! p{pno}: could not parse course line: {left!r}")
                        cur = None; continue
                    code, title = f"{m[2]} {m[3]}", m[4]
                    cur = {"comcode": cc[0], "course_code": code, "title": title,
                           "credits_raw": cols.get("credits", []),
                           "sections": [], "source": {"doc": "timetable.pdf", "page": pno}}
                    courses.append(cur); sec = None
                if cur is None: continue
                s = cols.get("sec", [])
                if s and SEC_RE.match(s[0]):                     # new section
                    sec = {"section": s[0],
                           "type": {"L": "lecture", "T": "tutorial", "P": "practical"}[s[0][0]],
                           "instructors": [" ".join(cols["instr"])] if cols.get("instr") else [],
                           "room": " ".join(cols.get("room", [])) or None,
                           "slots": parse_days_hours(cols.get("days", [])),
                           "cancelled": "CANCLED" in " ".join(cols.get("room", []) + cols.get("instr", []))}
                    cur["sections"].append(sec)
                    if cols.get("midsem"): cur["midsem"] = " ".join(cols["midsem"])
                    if cols.get("compre"): cur["compre"] = " ".join(cols["compre"])
                elif sec and cols.get("instr") and not cc:        # extra instructor line
                    sec["instructors"].append(" ".join(cols["instr"]))
    # Flag records we are not sure about instead of guessing (task section 3)
    for c in courses:
        issues = []
        if not c["title"]: issues.append("title not extracted")
        if len(c["credits_raw"]) != 5: issues.append("credit columns misaligned")
        c["needs_verification"] = issues
        c["only_2026_admissions"] = int(c["comcode"]) >= 5000  # note printed on every page
    return courses


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    data = parse(src)
    json.dump(data, open(out, "w"), indent=1)
    print(f"{len(data)} courses written to {out}")
