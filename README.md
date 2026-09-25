# BITS Academic Course Recommender

An agentic course recommender for BITS Pilani students (Postman AI/ML recruitment, Round 2).
It works out what a student is **required and eligible** to take from the official BITS
documents, and only then matches courses to the student's preferences.

**Design rule:** academic rules are checked by plain Python code, never by the LLM.
The LLM is used only for things code can't do well: reading free-text handouts,
understanding the student's query, and writing the explanation.

## Status

| Part | State |
|---|---|
| Timetable parser | ✅ done |
| Bulletin parser (CDC / DEL / HUEL lists) | ✅ done |
| Handout parser (LLM + code validation) | ✅ done, 538/540 extracted |
| Rules engine (remaining requirements, eligibility) | ✅ done for B.E. Computer Science |
| Query layer (LLM → filters → ranked results) | ✅ done, CLI only |
| Dashboard | ⏳ next |
| Timetable clash checking (brownie point) | ⏳ planned |

## Setup

Requires Python 3.10+ and a free Gemini API key from [aistudio.google.com](https://aistudio.google.com).

```bash
git clone https://github.com/prajeet0004/bits-recommender.git
cd bits-recommender
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (never committed):
```
GEMINI_API_KEY=your-key-here
GEMINI_MODELS=gemini-3.1-flash-lite,gemini-3.5-flash-lite
```
`GEMINI_MODELS` is a comma-separated list: the first is preferred, the rest are fallbacks
used when a model is overloaded or out of free quota.

Put the supplied dataset in `data/raw/` (it is not committed, ~240 MB):
```
data/raw/timetable.pdf
data/raw/bulletin.pdf
data/raw/Academic-Regulations-2023.pdf
data/raw/handouts/*.pdf
```

## Running the pipeline

The processed JSON files are already committed, so you can skip to step 4.
Steps 1–3 rebuild them from the raw PDFs.

```bash
# 1. Timetable -> data/processed/timetable.json            (~1 min, no LLM)
python ingest/parse_timetable.py data/raw/timetable.pdf data/processed/timetable.json

# 2. Bulletin course lists -> data/processed/course_lists.json   (~30 s, no LLM)
python ingest/parse_bulletin_lists.py data/raw/bulletin.pdf data/processed/course_lists.json

# 3. Handouts -> data/processed/handouts.jsonl   (~1 hr, uses Gemini, resumable)
python ingest/parse_handouts.py data/raw/handouts data/processed/handouts.jsonl
#    test on a few first:  add  --limit 5
#    re-run the code checks without calling the LLM:  add  --revalidate

# 4. Remaining requirements + eligible courses for a student profile
python core/rules.py data/students/example_cs_2025.json

# 5. Ask a question (2 LLM calls: understand the query, score interests)
python -m core.recommend data/students/example_cs_2025.json "Suggest DELs related to AI with no midsem"
```

Step 3 saves after every handout. If it stops (quota, network), run the same command
again and it continues from where it stopped; failed handouts are retried.

## Project layout

```
ingest/
  parse_timetable.py        timetable PDF -> sections, slots, rooms, exam dates
  parse_bulletin_lists.py   bulletin -> CDC/DEL lists per programme, HUEL pool
  parse_handouts.py         handouts -> evaluation, midsem, makeup, attendance, topics
core/
  rules.py                  deterministic: remaining requirements, eligible courses
  query.py                  LLM: question -> structured preferences (sees no course data)
  recommend.py              eligible set -> handout checks -> LLM interest scores -> results
  llm.py                    Gemini wrapper with model fallback
data/
  rules/programmes.json     requirement numbers per programme, with bulletin page refs
  students/                 example student profiles
  processed/                parsed, structured data (committed)
  raw/                      supplied PDFs (not committed)
```

## Data quality

Every extracted record keeps its source (document + page). Anything the code can't
confirm is marked `needs_verification` instead of guessed.

| Source | Records | Flagged for verification |
|---|---|---|
| Timetable | 728 course entries, 586 unique codes, 1709 sections | 7 |
| Bulletin lists | 28 programmes, 136 HUEL courses | 35 |
| Handouts | 540 files, 538 extracted | 61 (+2 scanned PDFs with no text) |

How much of each handout property is actually stated:

| Property | Known | Not stated |
|---|---|---|
| Midsem present? | 536 / 538 | 2 |
| Make-up policy | 430 / 538 | 108 |
| Attendance policy | 243 / 538 | 295 |

Attendance is stated in only 45% of handouts. For the rest, the system answers
"not stated in the handout" rather than assuming there is no requirement.

## Known limitations

- **Prerequisites are not verified.** The timetable points to an external website, and the
  bulletin lists prerequisites for only a few courses. Every recommendation says so.
- **Only B.E. Computer Science** has its requirement numbers in `programmes.json` so far.
  Adding a programme means adding one entry with page references; no code changes.
- **Equivalent courses** (timetable pp. 121–127) are not parsed yet, so a course taken
  under an old code is not yet recognised.
- **Two handouts are scanned images** (MAC F214, MATH F214) and have no extracted data.
