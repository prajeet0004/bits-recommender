"""The full recommendation flow from the task:

  profile + query
    -> requirement analysis & eligible set        (rules.py, code)
    -> query -> preferences                       (query.py, LLM)
    -> handout checks: midsem / attendance / ...  (code, on extracted handout data)
    -> interest matching                          (LLM scores only the eligible list)
    -> final list with reasons and sources        (code)

The LLM never adds a course: it only scores courses code already found eligible,
and any code it returns that is not in that list is thrown away.

Run:  python -m core.recommend data/students/example_cs_2025.json "Suggest DELs related to AI"
"""
import json
import re
import sys
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

from core import llm, rules
from core.query import Preferences, parse_query

HANDOUTS = rules.DATA / "processed" / "handouts.jsonl"
MAX_TO_SCORE = 150          # cap on courses sent to the LLM for interest scoring
YES, PARTLY, NO, UNKNOWN = "yes", "partly", "no", "not verified"
SOFT = {"project-based"}          # preferences: rank by them, never remove a course for them
EXPECTS_ATTENDANCE = re.compile(r"expected|must|mandatory|compulsory|required|as per|guidelines|regular", re.I)
PROJECT_LIKE = re.compile(r"project|assignment|term paper|presentation|seminar|report|portfolio", re.I)


# ---------- handout data ----------

def load_handouts(path=HANDOUTS):
    """course code -> best handout record. Prefers a record that passed validation.
    A clean handout that lists several codes (cross-listed courses) is used for all of them."""
    norm = lambda c: re.sub(r"\s+", " ", c.strip().upper())
    by_code = {}
    if not Path(path).exists():
        return by_code
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["extracted"]]
    rows.sort(key=lambda r: bool(r["needs_verification"]))       # clean records first
    for r in rows:
        by_code.setdefault(r["code"], r)
    for r in rows:
        if not r["needs_verification"]:
            for c in r["extracted"]["course_codes"]:
                by_code.setdefault(norm(c), r)
    return by_code


def check_constraints(pref: Preferences, h):
    """For each thing the student asked for, return (yes / no / not verified, evidence).
    'no' removes the course; 'not verified' keeps it but says so."""
    e = h["extracted"] if h else None
    out = {}

    def field(name):
        return None if e is None else e.get(name)

    if pref.no_midsem:
        v = field("has_midsem")
        out["no midsem"] = ((UNKNOWN, "no handout data") if v is None else
                            (YES, "no midsem in evaluation scheme") if v is False else
                            (NO, "has a midsem"))
    if pref.avoid_quizzes:
        v = field("has_quizzes")
        out["no quizzes"] = ((UNKNOWN, "not stated") if v is None else
                             (YES, "no quizzes") if v is False else (NO, "has quizzes"))
    if pref.no_attendance_requirement:
        marks, text = field("attendance_counts_for_marks"), field("attendance_policy")
        if marks is True:
            out["no attendance requirement"] = (NO, f"attendance carries marks: {text}")
        elif text is None:
            out["no attendance requirement"] = (UNKNOWN, "handout does not mention attendance")
        elif EXPECTS_ATTENDANCE.search(text):
            out["no attendance requirement"] = (PARTLY, f"no attendance marks, but handout says: {text}")
        else:
            out["no attendance requirement"] = (YES, f"no attendance marks; handout says: {text}")
    if pref.lenient_makeup:
        avail, prior = field("makeup_available"), field("makeup_needs_prior_permission")
        if avail is False:
            out["lenient makeup"] = (NO, "no make-up offered")
        elif avail is None:
            out["lenient makeup"] = (UNKNOWN, "make-up policy not stated")
        elif prior is True:
            out["lenient makeup"] = (NO, "make-up only with prior permission")
        elif field("makeup_needs_genuine_reason") is True:
            out["lenient makeup"] = (PARTLY, "no prior permission, but only for genuine reasons: "
                                     + (field("makeup_policy") or ""))
        else:
            out["lenient makeup"] = (YES, field("makeup_policy") or "make-up available")
    if pref.prefers_project:
        if e is None:
            out["project-based"] = (UNKNOWN, "no handout data")
        else:
            share = sum(c["weight_percent"] or 0 for c in e["evaluation"] if PROJECT_LIKE.search(c["component"]))
            status = YES if share >= 30 else PARTLY if share > 0 else NO
            out["project-based"] = (status, f"{share:.0f}% of marks from projects/assignments/presentations")
    return out


# ---------- interest matching (LLM) ----------

class Score(BaseModel):
    code: str
    relevance: int = Field(description="0 = unrelated, 10 = exactly what the student asked for")
    reason: str = Field(description="Max 15 words, based only on the title/topics given")


class Scores(BaseModel):
    scores: List[Score]


SCORE_PROMPT = """A student wants courses about: {interests}
Score how well each course below matches, using ONLY the title and topics shown.
Return one entry per course code. Do not add courses that are not in the list.

{lines}
"""


def score_interests(interests, candidates, handouts):
    """code -> (relevance 0-10, reason). Without interests every course scores 5."""
    if not interests:
        return {c["code"]: (5, "") for c in candidates}
    lines = []
    for c in candidates[:MAX_TO_SCORE]:
        h = handouts.get(c["code"])
        topics = ", ".join(h["extracted"]["topics"][:6]) if h else ""
        lines.append(f"{c['code']} | {c['title']} | {topics}")
    try:
        res = llm.ask(SCORE_PROMPT.format(interests=", ".join(interests), lines="\n".join(lines)), Scores)
    except Exception as e:                       # LLM down: fall back to keyword overlap
        print(f"(interest scoring failed, using keyword match: {e})", file=sys.stderr)
        return keyword_scores(interests, candidates, handouts)
    valid = {c["code"] for c in candidates}
    return {s.code: (max(0, min(10, s.relevance)), s.reason)
            for s in res.scores if s.code in valid}      # drop anything the LLM invented


def keyword_scores(interests, candidates, handouts):
    words = {w for i in interests for w in i.lower().split() if len(w) > 2}
    out = {}
    for c in candidates:
        h = handouts.get(c["code"])
        text = (c["title"] + " " + " ".join(h["extracted"]["topics"] if h else [])).lower()
        hits = sum(w in text for w in words)
        out[c["code"]] = (min(10, 3 * hits), "keyword match" if hits else "")
    return out


# ---------- the pipeline ----------

def lecture_slots(tt):
    lec = [s for s in tt["sections"] if s["type"] == "lecture" and s["slots"]]
    return [f"{s['section']}: {' '.join(s['slots']['days'])} hour {','.join(map(str, s['slots']['hours']))}"
            for s in lec]


def recommend(student, pref: Preferences, catalog=None, handouts=None):
    catalog = catalog or rules.Catalog()
    handouts = handouts if handouts is not None else load_handouts()
    state = rules.academic_state(student, catalog)
    eligible = rules.eligible_courses(student, catalog, state)
    rem = state["remaining"]
    offered = catalog.offered(student["admission_year"])
    notes = [f"Programme data: {w}" for w in state["warnings"]]

    cats = [pref.category] if pref.category else ["CDC", "DEL", "HUEL", "OPEL"]
    if pref.category in ("DEL", "HUEL", "OPEL") and rem[f"{pref.category}_units"] == 0:
        notes.append(f"Your {pref.category} requirement is already complete.")
    candidates = [c for cat in cats for c in eligible.get(cat, [])]

    kept, removed = [], 0
    for c in candidates:
        checks = check_constraints(pref, handouts.get(c["code"]))
        if any(v[0] == NO for k, v in checks.items() if k not in SOFT):
            removed += 1
            continue
        kept.append((c, checks))

    scores = score_interests(pref.interests, [c for c, _ in kept], handouts)

    results = []
    for c, checks in kept:
        rel, why = scores.get(c["code"], (0, ""))
        if pref.interests and rel < 4:
            continue                                   # not about what they asked for
        h = handouts.get(c["code"])
        # yes = 0, partly = -1, not verified = -3 ; project preference ranks by its share
        penalty = {YES: 0, PARTLY: 1, UNKNOWN: 3, NO: 0}
        rank = rel - sum(penalty[v[0]] for k, v in checks.items() if k not in SOFT)
        if "project-based" in checks:
            share = re.match(r"(\d+)%", checks["project-based"][1])
            rank += int(share[1]) / 10 if share else -3
        cat = c["counts_as"]
        left = (f"{len(rem['CDC_courses'])} CDCs left" if cat == "CDC"
                else f"{rem[cat + '_units']} {cat} units left")
        results.append({
            "code": c["code"], "title": c["title"], "units": c["units"],
            "requirement": f"counts as {cat} ({left})",
            "eligibility": "offered this semester, not yet taken; prerequisites not verified",
            "matches": why,
            "checks": {k: {"status": v[0], "evidence": v[1]} for k, v in checks.items()},
            "evaluation": [f"{e['component']} {e['weight_percent']}%" for e in h["extracted"]["evaluation"]] if h else [],
            "lectures": lecture_slots(offered[c["code"]]) if c["code"] in offered else [],
            "warnings": c["notes"] + (h["needs_verification"] if h else ["no handout found"]),
            "sources": [f"timetable.pdf p.{c['source']['page']}"] + ([h["source"]["doc"]] if h else []),
            "_rank": rank,
        })
    results.sort(key=lambda r: -r["_rank"])
    if removed:
        notes.append(f"{removed} eligible course(s) removed because their handout conflicts with your request.")
    return {"preferences": pref.model_dump(), "remaining": rem, "notes": notes,
            "results": results[: pref.count or 5]}


def format_text(out):
    lines = [f"Understood as: {json.dumps({k: v for k, v in out['preferences'].items() if v})}"]
    lines += [f"Note: {n}" for n in out["notes"]]
    if not out["results"]:
        lines.append("No eligible course matches this request.")
    for i, r in enumerate(out["results"], 1):
        lines.append(f"\n{i}. {r['code']} {r['title']} ({r['units']} units)")
        lines.append(f"   Requirement: {r['requirement']}")
        lines.append(f"   Eligibility: {r['eligibility']}")
        if r["matches"]:
            lines.append(f"   Why: {r['matches']}")
        for k, v in r["checks"].items():
            lines.append(f"   {k}: {v['status']} ({v['evidence'][:90]})")
        if r["evaluation"]:
            lines.append(f"   Evaluation: {', '.join(r['evaluation'])}")
        if r["lectures"]:
            lines.append(f"   Lectures: {'; '.join(r['lectures'])}")
        if r["warnings"]:
            lines.append(f"   Check: {'; '.join(r['warnings'])}")
        lines.append(f"   Source: {', '.join(r['sources'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    student = json.loads(Path(sys.argv[1]).read_text())
    question = " ".join(sys.argv[2:])
    print(format_text(recommend(student, parse_query(question))))
