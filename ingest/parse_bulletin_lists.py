"""Parse the Bulletin's course lists (pages IV-106 to IV-128) into JSON.

These pages list, for every first-degree programme, its CORE courses (CDC)
and DISCIPLINE ELECTIVE courses (DEL), then the pool of Humanities
electives (HUEL). The pages have two text columns, so each page is cut into
a left and right half and read as two separate columns, left first.

Reading is a small state machine:
  - UPPERCASE heading (e.g. COMPUTER SCIENCE)   -> new programme
  - "CORE COURSES" / "DISCIPLINE ELECTIVE ..."   -> switch category
  - "Pool of Humanities courses"                 -> HUEL pool starts
  - "Track - 1: ..." / "Pool – II: ..."          -> sub-group label
  - "DEPT F123 Title L P U"                      -> a course
  - any other short line after a course          -> title continued
"""
import json, re, sys
import pdfplumber

FIRST_PAGE, LAST_PAGE = 314, 336          # 1-indexed PDF pages (IV-106 .. IV-128)

COURSE_RE = re.compile(r"^(\*?)([A-Z]{2,5})\s+([A-Z]\d{3}[A-Z]?)\*?\s+(.*)$")
CODE_IN_TEXT = re.compile(r"\b[A-Z]{2,5}\s+[A-Z]\d{3}")
# trailing "3 0 3", "3 1 4", "4*", "3" ...  (L P U or just U)
UNITS_RE = re.compile(r"^(.*?)\s*(?:(\d+|-)\s+(\d+|-)\s+)?(\d+)(\*?)$")
GROUP_RE = re.compile(r"^(Track|Pool)\s*[-–]\s*[\dIVX]+\s*:?\s*(.*)$")
IGNORE = ("Course No", "Course Course", "No.", "L P U")


def columns(page):
    """Return the text lines of the left column, then the right column."""
    w, h = page.width, page.height
    out = []
    for x0, x1 in ((0, w / 2), (w / 2, w)):
        text = page.crop((x0, 0, x1, h)).extract_text() or ""
        out += [l.strip() for l in text.split("\n") if l.strip()]
    return out


def is_heading(line):
    letters = re.sub(r"[^A-Za-z]", "", line)
    return (len(letters) > 5 and line.upper() == line and not any(c.isdigit() for c in line)
            and not COURSE_RE.match(line))


def parse_course(line):
    m = COURSE_RE.match(line)
    if not m:
        return None
    star, dept, num, rest = m.groups()
    u = UNITS_RE.match(rest)
    title, L, P, U, ustar = (u.groups() if u else (rest, None, None, None, ""))
    return {"code": f"{dept} {num}", "title": title.strip(),
            "L": L, "P": P, "units": int(U) if U else None,
            "non_letter_grade": bool(star),          # '*' before code in the bulletin
            "units_flagged": bool(ustar)}             # '4*' style units, meaning unclear -> verify


def parse(path):
    programmes, huel, other = {}, [], []
    prog = cat = group = None
    target = None           # the list new courses are appended to
    last = None             # last course parsed (for title continuation)
    heading_buf = []

    def flush_heading(page_no):
        nonlocal prog, heading_buf
        if heading_buf:
            prog = " ".join(heading_buf)
            programmes.setdefault(prog, {"core": [], "electives": [], "pages": []})
            heading_buf = []

    with pdfplumber.open(path) as pdf:
        for page_no in range(FIRST_PAGE, LAST_PAGE + 1):
            for line in columns(pdf.pages[page_no - 1]):
                if re.fullmatch(r"IV-\d+", line) or line.startswith(IGNORE):
                    continue
                if line.startswith("List of Audit Type Courses"):
                    return programmes, huel, other
                if line.startswith("CORE COURSES") or line.startswith("DISCIPLINE ELECTIVE"):
                    flush_heading(page_no)
                    if prog is None or (line.startswith("CORE") and programmes[prog]["core"]):
                        # CORE COURSES with no heading before it: the programme name
                        # is missing from the text layer. Name it later from its courses.
                        prog = f"UNNAMED_{len(programmes)}"
                        programmes[prog] = {"core": [], "electives": [], "pages": []}
                    cat = "core" if line.startswith("CORE") else "electives"
                    target, group, last = programmes[prog][cat], None, None
                    if page_no not in programmes[prog]["pages"]:
                        programmes[prog]["pages"].append(page_no)
                    continue
                if line.startswith("Project Type Courses"):
                    target = last = None          # prose + 'XXX F266' placeholders follow
                    continue
                if line.startswith("Pool of Humanities"):
                    target, group, last = huel, None, None
                    continue
                if line.startswith("Other Courses"):
                    target, group, last = other, None, None
                    continue
                g = GROUP_RE.match(line)
                if g and target is not None:
                    group, last = line, None
                    continue
                if is_heading(line) and target is not huel and target is not other:
                    heading_buf.append(line)
                    target = last = None
                    continue
                c = parse_course(line)
                if c and target is not None and not c["code"].startswith("XXX"):
                    c["group"] = group
                    c["source"] = {"doc": "bulletin.pdf", "page": page_no}
                    target.append(c)
                    last = c
                    continue
                if last is not None and len(line) < 45 and not line.startswith("It may be"):
                    # wrapped title: 'Neural Networks and Fuzzy' + 'Logic'
                    last["title"] = f"{last['title']} {line}".strip()
                else:
                    last = None
    return programmes, huel, other


def name_unnamed(programmes):
    """A programme whose heading was missing: name it by its most common
    core-course prefix and flag it, rather than silently guessing."""
    for name in [n for n in programmes if n.startswith("UNNAMED_")]:
        prefixes = [c["code"].split()[0] for c in programmes[name]["core"]]
        guess = max(set(prefixes), key=prefixes.count) if prefixes else "UNKNOWN"
        p = programmes.pop(name)
        p["needs_verification"] = f"programme heading missing in PDF; named from course prefix '{guess}'"
        programmes[f"{guess} (inferred)"] = p


def flag_suspicious(programmes, huel, other):
    """Mark records the parser may have got wrong, so a human can check them."""
    lists = [c for p in programmes.values() for c in p["core"] + p["electives"]] + huel + other
    for c in lists:
        issues = []
        if CODE_IN_TEXT.search(c["title"]): issues.append("another course code inside title (merged lines?)")
        if c["units"] is None: issues.append("units not found")
        if c["units_flagged"]: issues.append("units marked with * in bulletin")
        c["needs_verification"] = issues
    return sum(bool(c["needs_verification"]) for c in lists)


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    programmes, huel, other = parse(src)
    name_unnamed(programmes)
    n_flagged = flag_suspicious(programmes, huel, other)
    json.dump({"programmes": programmes, "huel_pool": huel, "other_courses": other},
              open(out, "w"), indent=1)
    print(f"{len(programmes)} programmes, {len(huel)} HUEL courses, {len(other)} other courses -> {out}")
    print(f"{n_flagged} records flagged needs_verification")
    for n, p in programmes.items():
        print(f"  {n:60s} core={len(p['core']):3d}  electives={len(p['electives']):3d}")
