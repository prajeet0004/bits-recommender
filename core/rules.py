"""Deterministic academic rules: no LLM in this file.

Given a student profile, work out
  1. what each completed/current course counts towards (CDC, DEL, HUEL, OPEL, GIR)
  2. what is still remaining in each category
  3. which courses OFFERED THIS SEMESTER the student may take, per category

Anything we cannot check from the data (e.g. prerequisites) is reported as
'not verified' instead of being assumed.
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def load_json(rel):
    return json.loads((DATA / rel).read_text())


class UnsupportedProgramme(ValueError):
    pass


class Catalog:
    """Everything loaded once from data/: programme rules, course lists, timetable."""

    def __init__(self):
        self.programmes = load_json("rules/programmes.json")
        lists = load_json("processed/course_lists.json")
        self.course_lists = lists["programmes"]
        self.huel_pool = {c["code"]: c for c in lists["huel_pool"]}
        self.timetable = load_json("processed/timetable.json")

    def supported_programmes(self):
        return sorted(k for k in self.programmes if not k.startswith("_"))

    def programme(self, name):
        """Rules + course lists for a programme, or a clear error (not a KeyError)."""
        if name not in self.programmes or name.startswith("_"):
            raise UnsupportedProgramme(
                f"'{name}' is not supported yet (dual degrees are not supported). "
                f"Supported: {', '.join(self.supported_programmes())}")
        rules = self.programmes[name]
        return rules, self.course_lists[rules["course_list_key"]]

    def programme_warnings(self, name):
        """Cross-check two sources that were extracted independently:
        the CDC count typed from the semester pattern vs the parsed CDC list."""
        rules, plist = self.programme(name)
        warn = list(rules.get("needs_verification", []))
        want, got = rules["requirements"]["CDC"]["courses"], len(plist["core"])
        if want != got:
            warn.append(f"bulletin says {want} CDCs but the parsed CDC list has {got}; "
                        f"remaining-CDC list may be wrong")
        offered = {c["course_code"] for c in self.timetable}
        if not any(c["code"] in offered for c in plist["core"]):
            warn.append("none of this programme's CDC codes appear in this semester's timetable "
                        "(may be offered under other codes, e.g. via the equivalent-courses table, "
                        "which is not parsed yet)")
        return warn

    def offered(self, admission_year):
        """course_code -> timetable record, for courses running this semester.
        Skips courses whose every lecture section is cancelled, and com codes
        >= 5000 unless the student was admitted in 2026 (note in timetable)."""
        out = {}
        for c in self.timetable:
            if c["only_2026_admissions"] and admission_year != 2026:
                continue
            lectures = [s for s in c["sections"] if s["type"] == "lecture"]
            if lectures and all(s["cancelled"] for s in lectures):
                continue
            out.setdefault(c["course_code"], c)
        return out


def gir_codes(prog_rules):
    """Flatten the GIR table. ['ECON F211', 'MGTS F211'] means 'either one'."""
    codes = set()
    for head, items in prog_rules["gir_courses"].items():
        if head.startswith("_"):
            continue
        for item in items:
            codes.update(item if isinstance(item, list) else [item])
    return codes


def units_of(code, catalog, prog_list):
    """Units for a course: bulletin list first, else timetable, else None."""
    for c in prog_list["core"] + prog_list["electives"]:
        if c["code"] == code and c["units"]:
            return c["units"]
    if code in catalog.huel_pool and catalog.huel_pool[code]["units"]:
        return catalog.huel_pool[code]["units"]
    for c in catalog.timetable:
        if c["course_code"] == code and c["credits_raw"] and c["credits_raw"][-1].isdigit():
            return int(c["credits_raw"][-1])
    return None


def classify(code, prog_rules, prog_list, catalog, gir):
    """Which category a course counts towards for this programme.
    Order matters: CDC > GIR > DEL > HUEL > OPEL."""
    core = {c["code"] for c in prog_list["core"]}
    dels = {c["code"] for c in prog_list["electives"]}
    if code in core:
        return "CDC"
    if code in gir:
        return "GIR"
    if code in dels:
        return "DEL"
    own = code.split()[0] in prog_rules["discipline_prefixes"]
    if code in catalog.huel_pool and not own:   # bulletin p.335: own-discipline course can't be HUEL
        return "HUEL"
    return "OPEL"


def academic_state(student, catalog):
    prog_rules, prog_list = catalog.programme(student["programme"])
    req = prog_rules["requirements"]
    gir = gir_codes(prog_rules)

    taken = student["completed"] + student["current"]
    done_units = {"CDC": 0, "DEL": 0, "HUEL": 0, "OPEL": 0, "GIR": 0}
    counted, unknown_units = {}, []

    for code in taken:
        cat = classify(code, prog_rules, prog_list, catalog, gir)
        u = units_of(code, catalog, prog_list)
        if u is None:
            unknown_units.append(code)
            u = 0
        # Extra DEL / HUEL beyond the requirement overflow into OPEL.
        if cat in ("DEL", "HUEL") and done_units[cat] >= req[cat]["units"]:
            cat = "OPEL"
        done_units[cat] += u
        counted[code] = cat

    core_codes = [c["code"] for c in prog_list["core"]]
    remaining = {
        "CDC_courses": [c for c in core_codes if c not in taken],
        "DEL_units": max(0, req["DEL"]["units"] - done_units["DEL"]),
        "HUEL_units": max(0, req["HUEL"]["units"] - done_units["HUEL"]),
        "OPEL_units": max(0, req["OPEL"]["units"] - done_units["OPEL"]),
        "GIR_courses": sorted(c for c in gir if c not in taken
                              and not ({"ECON F211", "MGTS F211"} & set(taken) and c in ("ECON F211", "MGTS F211"))),
    }
    return {"counted_as": counted, "units_done": done_units, "remaining": remaining,
            "units_unknown_for": unknown_units, "sources": {k: v["source"] for k, v in req.items()},
            "warnings": catalog.programme_warnings(student["programme"])}


PROJECT_NUMBERS = {"F266", "F366", "F367", "F376", "F377", "F491"}


def course_notes(code):
    """Things the data says we should warn about, but cannot decide automatically."""
    notes = []
    num = code.split()[1]
    if num.startswith("G"):
        notes.append("higher-degree (G) course: check if first-degree students may register")
    if num in PROJECT_NUMBERS:
        notes.append("project course: max 3 as OPEL, max 5 across electives (bulletin p.333)")
    return notes


def eligible_courses(student, catalog, state=None):
    """Courses offered this semester that the student may take, grouped by the
    category they would count towards. Prerequisites are NOT verified here
    (the supplied data only has them for a few courses) and are marked so."""
    state = state or academic_state(student, catalog)
    prog_rules, prog_list = catalog.programme(student["programme"])
    gir = gir_codes(prog_rules)
    taken = set(student["completed"] + student["current"])
    rem = state["remaining"]

    out = {"CDC": [], "DEL": [], "HUEL": [], "OPEL": [], "GIR": []}
    for code, tt in catalog.offered(student["admission_year"]).items():
        if code in taken:
            continue
        cat = classify(code, prog_rules, prog_list, catalog, gir)
        if cat == "GIR" and code not in rem["GIR_courses"]:
            continue
        # a DEL/HUEL the student no longer needs still counts as an OPEL
        if cat in ("DEL", "HUEL") and rem[f"{cat}_units"] == 0:
            cat = "OPEL"
        out[cat].append({
            "code": code,
            "title": tt["title"],
            "units": units_of(code, catalog, prog_list),
            "counts_as": cat,
            "prerequisites": "not verified",
            "notes": course_notes(code),
            "source": tt["source"],
        })
    if rem["OPEL_units"] == 0:
        out["OPEL"] = []          # nothing left to fill in this category
    return out


if __name__ == "__main__":
    import sys
    student = json.loads(Path(sys.argv[1]).read_text())
    cat = Catalog()
    st = academic_state(student, cat)
    print("Units done:", st["units_done"])
    print("Remaining:", json.dumps(st["remaining"], indent=1))
    for w in st["warnings"]:
        print("Warning:", w)
    if st["units_unknown_for"]:
        print("Units unknown (check by hand):", st["units_unknown_for"])
    el = eligible_courses(student, cat, st)
    for k, v in el.items():
        print(f"\n{k}: {len(v)} offered & eligible  e.g. {[c['code'] for c in v[:8]]}")
