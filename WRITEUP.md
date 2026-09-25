# Writeup — BITS Academic Course Recommender

*Living document, updated as the project develops.*

## 1. The core idea

The task says a course must be **academically valid first** and only then matched to
interests. So the system is split in two:

- **Facts and rules → plain Python.** Which courses are CDCs, how many DEL units are left,
  what's offered this semester. These have one correct answer, must be the same every run,
  and must be testable. An LLM can miscount or invent a course; code can't.
- **Language → LLM.** Reading free-text handouts, understanding "AI DEL with no midsem",
  and explaining results. Code is bad at these; LLMs are good.

The LLM never decides eligibility. It only explains what the code already decided.

## 2. Looking at the data first

Before writing code I inspected every supplied document. What I found shaped the design:

| Document | What matters | Difficulty |
|---|---|---|
| Timetable (153 pp.) | Course table on pp. 10–112; equivalent courses on pp. 121–127 | Clean table |
| Bulletin (951 pp.) | Programme structures, CDC/DEL lists, HUEL pool, course descriptions | Two-column layout |
| Regulations (70 pp.) | Elective counting rules | Prose |
| Handouts (540 PDFs) | Evaluation, midsem, makeup, attendance, topics | Free text, inconsistent |

Findings that changed the design:

1. **Prerequisites are mostly missing.** The timetable only points to a website. So the
   rules engine reports "prerequisite not verified" instead of assuming there are none.
2. **Course codes repeat in the timetable** (728 entries, 586 unique codes). Com codes
   ≥ 5000 are only for 2026 admits; the parser flags them and the rules engine skips them
   for other batches.
3. **Only 2 of 540 handouts are scanned**, so text extraction works for almost everything.

## 3. Parsing: position-based, not text-based

PDF tables have no real "cells" — just words with x/y positions. Both non-LLM parsers
use this:

- **Timetable:** column edges are read from each page's header; each word goes to a column
  by its x-position, and to a line by its y-position.
- **Bulletin:** pages are cut into left and right halves and read as two columns, then a
  small state machine tracks the current programme and category (CORE / DISCIPLINE ELECTIVE).

A bug worth noting: I first grouped words into lines by rounding y to a grid. Words that
straddled a grid boundary split into two lines, which left 68 course titles empty.
Grouping by distance from the line's first word (tolerance 2.5 pt) fixed it.

## 4. Handouts: LLM extraction, code validation

Handouts vary too much for regex, so Gemini fills a fixed schema (Pydantic). The prompt
says to use `null` for anything not written, never a guess. Then **code checks the output**:

- the course code on the handout must match the filename,
- evaluation weights must add up to ~100,
- the midsem flag must agree with the evaluation table.

Anything that fails is kept but marked `needs_verification`.

**Checking against the source.** Before running all 540, I compared BIO F101's output
with its PDF by hand. The evaluation table, weights and midsem were all correct, but the
makeup field was a single choice while the handout had **two** conditions (genuine reason
*and* prior permission), and "no make-up for quizzes" had nowhere to go. I replaced it
with four yes/no fields. Catching this on 1 file cost nothing; catching it after 540
would have meant re-running everything.

**The validator found real errors in the supplied dataset:**

- CS / ECE / EEE / INSTR F215 files all contain the F342 handout
- BIO G523 contains BIO F212
- BITS F101-1 contains SW E101
- MPBA G520 contains MPBA G521

## 5. Engineering problems with the free LLM tier

The handout run hit several real-world failures, each of which changed the code:

| Problem | Fix |
|---|---|
| Model overloaded (503) for minutes | Retry with backoff, then fall back to the next model |
| Daily free quota (429) hit at ~435 requests | Try the next model at once; stop cleanly only if all models are out |
| Fallback model had been removed (404) | Treat 404 like 429: skip the model |
| A request hung forever | 90-second timeout |
| Failed files were saved as "done", so reruns skipped them | Only successful results count as done; failures are retried |
| Codes like `BITS F 429`, `CEG551` falsely flagged | Compare codes with spaces removed; `--revalidate` re-checks saved rows without LLM calls |

The run is resumable: results are written one handout at a time, so a crash at 300
restarts at 301.

## 6. Rules engine

`core/rules.py`, no LLM. For a student profile it:

1. classifies each taken course as CDC, GIR, DEL, HUEL or OPEL (in that order of priority;
   DEL/HUEL beyond the requirement overflow into OPEL),
2. computes what is left in each category,
3. lists courses offered this semester the student may take, grouped by what they would
   count towards.

Requirement numbers (48 CDC units, 12 DEL, 8 HUEL, 15 OPEL for CS) are copied by hand
into `data/rules/programmes.json` with bulletin page references. They are ~10 numbers per
programme sitting in messy tables, so copying and citing them is more reliable than
parsing. Course **lists** still come from the parsed bulletin, so nothing about which
courses count is hardcoded.

### Extending to all B.E. programmes

The rules engine was built and tested on one programme (Computer Science) first, then
extended to all 17 single-degree B.E. programmes by adding their requirement numbers
from each semester-wise pattern (bulletin pp. 211–227). No code changed, only data.

Adding them exposed problems that one programme never would, so the engine now
**cross-checks two independently extracted sources** for every programme:

- the CDC count typed from the semester pattern vs the length of the parsed CDC list:
  mismatches for EEE (15 vs 14: the pattern says "47 or 48 units", so one CDC is an
  either/or) and Environmental & Sustainability (13 vs 16);
- whether any of the programme's CDC codes appear in this semester's timetable: none do
  for Biotechnology, Electronics & Computer, and Robotics & Industrial Automation, whose
  CDCs likely run under other codes via the (not yet parsed) equivalent-courses table.

These are shown to the student as warnings rather than hidden. Dual degrees combine two
programmes' discipline requirements with sharing rules (IV-1), which needs real logic,
not just data, so they are out of scope and give a clear "not supported" message.

## 7. Query layer

A question goes through five steps, and the LLM is used in exactly two:

1. **Rules engine (code)** builds the eligible set for the student.
2. **Query parsing (LLM)** turns the question into a fixed `Preferences` form
   (category, interests, no_midsem, no_attendance_requirement, lenient_makeup, ...).
   The LLM sees no course data here, so it cannot pick or invent courses.
3. **Handout checks (code).** Each request becomes a three-way check per course:
   `yes`, `no` or `not verified`. `no` removes the course; `not verified` keeps it,
   says so, and ranks it lower.
4. **Interest scoring (LLM).** The LLM scores only the courses code already found eligible,
   using their titles and handout topics. Any course code it returns that isn't in that
   list is discarded. If the LLM call fails, a keyword match is used instead.
5. **Output (code).** Each result states the requirement it fills, eligibility, the checks
   with evidence, evaluation scheme, lecture slots, warnings and source documents.

Explanations are built by code from stored facts, not written freely by the LLM. The only
LLM-written text in an answer is the short "why it matches your interest" line. This keeps
the answer traceable, which matters more here than fluent prose.

**Checks have four outcomes, not two.** The first real run showed that yes/no was too
coarse: "OPEL with no attendance requirement" returned courses whose handout says
*"each student is expected to attend all classes"*, because attendance carried no marks.
That is not what a student means by "no attendance requirement". So each check is now:

| Status | Meaning | Effect |
|---|---|---|
| yes | clearly satisfies the request | ranked first |
| partly | satisfies it with a catch (e.g. no marks, but attendance expected) | kept, ranked lower, catch shown |
| not verified | handout doesn't say | kept, ranked lower, stated as unverified |
| no | conflicts with the request | removed |

Definitions used:
- **No attendance requirement:** attendance carries no marks *and* the handout doesn't say
  attendance is expected or governed by institute guidelines. Otherwise "partly".
- **Lenient make-up:** make-up offered without prior permission. If it's only for genuine
  reasons (the normal BITS rule), "partly".
- **Prefers project-based:** a *preference*, not a filter. Courses are ranked by the share
  of marks from projects, assignments and presentations, computed from the evaluation table.
  The first version only used a yes/no `has_project` flag, and every HUEL it returned had
  a 30% midsem and no project at all.

## 8. What the data can and can't answer

| Property | Known | Not stated |
|---|---|---|
| Midsem present? | 536 / 538 | 2 |
| Make-up policy | 430 / 538 | 108 |
| Attendance policy | 243 / 538 | 295 |

Most handouts say nothing about attendance. So "an OPEL with no attendance requirement"
can only be answered with confidence for about half the courses; for the rest the system
must say "not stated in the handout" — which is what the task asks for.

## 9. Known gaps

- Prerequisites not verified (data missing).
- Equivalent-course table not parsed yet.
- Only B.E. Computer Science configured.
- Two scanned handouts have no data.
- The GIR "either ECON F211 or MGTS F211" rule is counted correctly but displayed as two
  separate remaining courses.
- Eligible CDC lists include later-year courses; ranking should prefer courses that fit
  the student's year in the semester-wise pattern.
