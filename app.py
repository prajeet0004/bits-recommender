"""Dashboard: build a profile, see remaining requirements, ask for courses.

All logic lives in core/. This file only draws the screen and calls:
  rules.academic_state()   remaining requirements   (code)
  query.parse_query()      question -> preferences   (LLM)
  recommend.recommend()    eligible -> ranked list   (code + LLM scoring)

Run:  streamlit run app.py
"""
import html
import json
import os
import re
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="BITS Course Recommender", page_icon="🎓", layout="wide")

# On Streamlit Cloud the key comes from st.secrets; locally from .env (core/llm.py)
try:
    for k in ("GEMINI_API_KEY", "GEMINI_MODELS"):
        if k in st.secrets:
            os.environ[k] = st.secrets[k]
except Exception:
    pass

from core import rules                                          # noqa: E402
from core.query import parse_query                              # noqa: E402
from core.recommend import load_handouts, recommend             # noqa: E402

STUDENTS = rules.DATA / "students"
EXAMPLES = [
    "Suggest DELs related to AI with no midsem",
    "I want an OPEL with no attendance requirement",
    "Suggest courses with no midsem and a lenient makeup policy",
    "I need a HUEL and prefer project-based evaluation",
]

# ---------- look ----------

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap');
html, body, [class*="css"], .stMarkdown, .stTextInput, .stButton, .stSelectbox, .stMultiSelect {
  font-family: 'Figtree', system-ui, sans-serif;
}
:root {
  --ink: #172033; --muted: #5B6478; --line: #DDE2EA; --card: #FFFFFF;
  --yes: #2E7D55; --partly: #B97D0C; --unknown: #6B7385; --no: #B23A2E; --blue: #2B4C9B;
}
.block-container { padding-top: 2rem; max-width: 1100px; }
h1.title { font-family: 'Newsreader', Georgia, serif; font-weight: 600; font-size: 2.3rem;
  color: var(--ink); margin: 0 0 .15rem 0; letter-spacing: -0.01em; }
p.sub { color: var(--muted); margin: 0 0 1.6rem 0; font-size: 1rem; }

/* requirement ledger: the one bold element */
.ledger { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1.1rem 1.3rem .6rem; margin-bottom: 1rem; }
.lrow { display: grid; grid-template-columns: 9.5rem 1fr 7.5rem; align-items: center;
  gap: 1rem; padding: .55rem 0; border-bottom: 1px solid #EEF1F5; }
.lrow:last-child { border-bottom: none; }
.lname { font-weight: 600; color: var(--ink); }
.lname small { display: block; font-weight: 400; color: var(--muted); font-size: .78rem; }
.track { display: flex; gap: 3px; height: 12px; }
.seg { flex: 1; border-radius: 2px; background: #E4E8EF; }
.seg.done { background: var(--blue); }
.lnum { font-family: 'Newsreader', Georgia, serif; font-size: 1.35rem; text-align: right; color: var(--ink); }
.lnum span { font-family: 'Figtree', sans-serif; font-size: .8rem; color: var(--muted); }

.warn { border-left: 3px solid var(--partly); background: #FFF8EA; padding: .5rem .8rem;
  margin: .35rem 0; border-radius: 0 6px 6px 0; font-size: .9rem; color: #5A4210; }

/* results */
.understood { color: var(--muted); font-size: .92rem; margin: .4rem 0 1rem; }
.understood b { color: var(--ink); font-weight: 600; }
.course { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.2rem; margin-bottom: .8rem; }
.chead { display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; }
.ccode { font-weight: 700; color: var(--blue); margin-right: .45rem; }
.ctitle { font-weight: 600; color: var(--ink); }
.cunits { color: var(--muted); font-size: .85rem; white-space: nowrap; }
.creq { color: var(--muted); font-size: .88rem; margin: .2rem 0 .6rem; }
.why { color: var(--ink); font-size: .93rem; margin-bottom: .55rem; }
.check { display: flex; gap: .55rem; align-items: baseline; font-size: .88rem; margin: .2rem 0; }
.pill { font-size: .72rem; font-weight: 700; padding: .1rem .5rem; border-radius: 99px;
  white-space: nowrap; }
.pill.yes { background: #E3F2EA; color: var(--yes); }
.pill.partly { background: #FBEFD6; color: var(--partly); }
.pill.unknown { background: #ECEEF2; color: var(--unknown); }
.meta { font-size: .84rem; color: var(--muted); margin-top: .55rem; line-height: 1.5; }
.meta b { color: var(--ink); font-weight: 600; }
.cwarn { font-size: .82rem; color: #7A5A12; margin-top: .4rem; }
</style>
""", unsafe_allow_html=True)


# ---------- data (loaded once) ----------

@st.cache_resource
def catalog():
    return rules.Catalog()


@st.cache_resource
def handouts():
    return load_handouts()


@st.cache_data
def course_options():
    """'CODE  Title' for every course we know, for the course pickers."""
    cat = catalog()
    titles = {}
    for c in cat.timetable:
        titles.setdefault(c["course_code"], c["title"].title())
    for p in cat.course_lists.values():
        for c in p["core"] + p["electives"]:
            titles.setdefault(c["code"], c["title"])
    for c in cat.huel_pool.values():
        titles.setdefault(c["code"], c["title"])
    for r in cat.programmes.values():
        if isinstance(r, dict):
            for items in r.get("gir_courses", {}).values():
                if isinstance(items, list):
                    for i in items:
                        for code in (i if isinstance(i, list) else [i]):
                            titles.setdefault(code, "")
    return {code: f"{code}  {t}".strip() for code, t in sorted(titles.items())}


def saved_profiles():
    return sorted(p.stem for p in STUDENTS.glob("*.json"))


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "student"


# ---------- sidebar: profile ----------

cat = catalog()
opts = course_options()
programmes = cat.supported_programmes()

with st.sidebar:
    st.markdown("### Your profile")
    names = saved_profiles()
    choice = st.selectbox("Load a saved profile", ["New profile"] + names,
                          index=(names.index("example_cs_2025") + 1) if "example_cs_2025" in names else 0)
    base = json.loads((STUDENTS / f"{choice}.json").read_text()) if choice != "New profile" else {}

    name = st.text_input("Profile name", key=f"name_{choice}", value="" if choice == "New profile" else choice)
    campus = st.selectbox("Campus", ["Pilani"], key=f"campus_{choice}", help="The supplied timetable is for Pilani.")
    prog = st.selectbox("Programme", programmes, key=f"prog_{choice}",
                        index=programmes.index(base["programme"]) if base.get("programme") in programmes else 0,
                        help="Single-degree B.E. programmes. Dual degrees are not supported yet.")
    c1, c2 = st.columns(2)
    year = c1.number_input("Admission year", 2015, 2026, key=f"year_{choice}", value= int(base.get("admission_year", 2025)))
    sem = c2.number_input("Current semester", 1, 8, key=f"sem_{choice}", value= int(base.get("current_semester", 3)))

    known = list(opts)
    completed = st.multiselect("Completed courses", known, key=f"done_{choice}",
                               default=[c for c in base.get("completed", []) if c in opts],
                               format_func=lambda c: opts[c])
    current = st.multiselect("Courses you're taking now", known, key=f"now_{choice}",
                             default=[c for c in base.get("current", []) if c in opts],
                             format_func=lambda c: opts[c])
    minor = st.text_input("Minor (optional)", key=f"minor_{choice}", value=base.get("minor") or "",
                          help="Stored with your profile. Minor requirements are not checked yet.")
    interests = st.text_input("Interests, comma separated", key=f"int_{choice}",
                              value=", ".join(base.get("interests", [])))

    student = {"campus": campus, "admission_year": int(year), "programme": prog,
               "current_semester": int(sem), "completed": completed, "current": current,
               "minor": minor or None,
               "interests": [i.strip() for i in interests.split(",") if i.strip()]}

    if st.button("Save profile", use_container_width=True, disabled=not name.strip()):
        STUDENTS.mkdir(parents=True, exist_ok=True)
        (STUDENTS / f"{slug(name)}.json").write_text(json.dumps(student, indent=1))
        st.success(f"Saved as {slug(name)}")
    st.caption("Requirements below update as you edit; saving is only needed to reload later.")


# ---------- main: header + ledger ----------

st.markdown('<h1 class="title">What should you take this semester?</h1>', unsafe_allow_html=True)
st.markdown(f'<p class="sub">{html.escape(prog)}, admitted {year}, semester {sem}. '
            'Recommendations come only from courses you are eligible for.</p>', unsafe_allow_html=True)

try:
    state = rules.academic_state(student, cat)
except rules.UnsupportedProgramme as e:
    st.error(str(e))
    st.stop()

req = cat.programme(prog)[0]["requirements"]
rem = state["remaining"]


def row(label, sub, done, total, unit):
    done = max(0, min(done, total))
    segs = "".join(f'<div class="seg{" done" if i < done else ""}"></div>' for i in range(total))
    return (f'<div class="lrow"><div class="lname">{label}<small>{sub}</small></div>'
            f'<div class="track">{segs}</div>'
            f'<div class="lnum">{done}<span> / {total} {unit}</span></div></div>')


cdc_total = req["CDC"]["courses"]
cdc_done = cdc_total - len(rem["CDC_courses"])
ledger = [row("CDC", "discipline core", cdc_done, cdc_total, "courses")]
for k, sub in (("DEL", "discipline electives"), ("HUEL", "humanities electives"), ("OPEL", "open electives")):
    t = req[k]["units"]
    ledger.append(row(k, sub, t - rem[f"{k}_units"], t, "units"))
st.markdown('<div class="ledger">' + "".join(ledger) + "</div>", unsafe_allow_html=True)
st.caption("Counts include courses you're taking now. Sources: " +
           "; ".join(f"{k} {v}" for k, v in state["sources"].items()))

gir_left = rem["GIR_courses"]
if {"ECON F211", "MGTS F211"} <= set(gir_left):
    gir_left = [c for c in gir_left if c not in ("ECON F211", "MGTS F211")] + ["ECON F211 or MGTS F211"]
with st.expander(f"Remaining CDCs ({len(rem['CDC_courses'])}) and general requirements ({len(gir_left)})"):
    st.write("**CDCs:** " + (", ".join(rem["CDC_courses"]) or "none left"))
    st.write("**General institutional (GIR):** " + (", ".join(gir_left) or "none left"))
    if state["units_unknown_for"]:
        st.write("**Units not found in this semester's data** (not counted): " +
                 ", ".join(state["units_unknown_for"]))

for w in state["warnings"]:
    st.markdown(f'<div class="warn">{html.escape(w)}</div>', unsafe_allow_html=True)


# ---------- ask ----------

st.markdown("#### Ask for courses")
cols = st.columns(2) * 2
for col, ex in zip(cols, EXAMPLES):
    if col.button(ex, use_container_width=True):
        st.session_state.question = ex
        st.session_state.run = True          # an example runs straight away
with st.form("ask", border=False):
    q = st.text_input("Your question", key="question", label_visibility="collapsed",
                      placeholder="e.g. Suggest an AI-related DEL with no midsem")
    go = st.form_submit_button("Find courses", type="primary")


def check_html(k, v):
    cls = {"yes": "yes", "partly": "partly"}.get(v["status"], "unknown")
    return (f'<div class="check"><span class="pill {cls}">{html.escape(v["status"])}</span>'
            f'<span><b>{html.escape(k)}</b>: {html.escape(v["evidence"][:160])}</span></div>')


def card(r):
    checks = "".join(check_html(k, v) for k, v in r["checks"].items())
    why = f'<div class="why">{html.escape(r["matches"])}</div>' if r["matches"] else ""
    meta = []
    if r["evaluation"]:
        meta.append("<b>Evaluation</b> " + html.escape(", ".join(r["evaluation"])))
    if r["lectures"]:
        meta.append("<b>Lectures</b> " + html.escape("; ".join(r["lectures"])))
    meta.append("<b>Eligibility</b> " + html.escape(r["eligibility"]))
    meta.append("<b>Source</b> " + html.escape(", ".join(r["sources"])))
    warn = (f'<div class="cwarn">Check: {html.escape("; ".join(r["warnings"]))}</div>'
            if r["warnings"] else "")
    return (f'<div class="course"><div class="chead"><div><span class="ccode">{html.escape(r["code"])}</span>'
            f'<span class="ctitle">{html.escape(r["title"].title())}</span></div>'
            f'<div class="cunits">{r["units"] or "?"} units</div></div>'
            f'<div class="creq">{html.escape(r["requirement"][:1].upper() + r["requirement"][1:])}</div>'
            f'{why}{checks}<div class="meta">{"<br>".join(meta)}</div>{warn}</div>')


if st.session_state.pop("run", False):
    go, q = True, st.session_state.question
if go and q.strip():
    with st.spinner("Reading your question and checking eligible courses…"):
        try:
            pref = parse_query(q)
            out = recommend(student, pref, catalog=cat, handouts=handouts())
        except Exception as e:
            st.error(f"The language model could not be reached ({str(e)[:160]}). "
                     "Check GEMINI_API_KEY and GEMINI_MODELS, then try again.")
            st.stop()
    p = {k: v for k, v in out["preferences"].items() if v and k != "count"}
    st.markdown('<div class="understood">Understood as: ' +
                ", ".join(f"<b>{html.escape(k.replace('_', ' '))}</b> {html.escape(", ".join(v) if isinstance(v, list) else str(v)) if v is not True else ''}"
                           for k, v in p.items()) + "</div>", unsafe_allow_html=True)
    for n in out["notes"]:
        if not n.startswith("Programme data:"):
            st.caption(n)
    if not out["results"]:
        st.info("No eligible course matches this. Try removing one condition, or ask without a category.")
    for r in out["results"]:
        st.markdown(card(r), unsafe_allow_html=True)
