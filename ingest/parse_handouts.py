"""Extract structured facts from course handouts.

Pipeline for each handout PDF:
  1. pdfplumber pulls out the text (no LLM).
  2. Gemini fills a FIXED schema (the Handout class below) from that text.
     It is told to use null for anything the handout doesn't say.
  3. Plain code checks the result (weights add to ~100, course code matches
     the filename, midsem flag agrees with the evaluation table) and writes
     any problems into `needs_verification` instead of hiding them.

Results are appended to a .jsonl file one handout at a time, so if the run
stops (quota, network), running it again continues where it left off.
Failed calls are not saved as done, so they are retried on the next run.
Set GEMINI_MODELS in .env as a comma list: first = preferred, rest = fallbacks.

Usage:
  python ingest/parse_handouts.py data/raw/handouts data/processed/handouts.jsonl --limit 5
"""
import argparse, json, os, re, sys, time
from pathlib import Path
from typing import List, Literal, Optional

import pdfplumber
from dotenv import load_dotenv
from pydantic import BaseModel, Field

MAX_CHARS = 15000          # handouts are ~3-6k chars; cap protects against huge ones
MIN_WORDS = 100            # fewer words than this = scanned image, no text layer


# ---------- the schema Gemini must fill ----------

class EvalComponent(BaseModel):
    component: str = Field(description="Name as written, e.g. 'Mid-Semester Test', 'Quiz 1', 'Project'")
    weight_percent: Optional[float] = Field(description="Weight in percent of total marks. Convert marks to percent if needed. null if not stated.")
    open_book: Optional[bool] = Field(description="true = open book, false = closed book, null if not stated")


class Handout(BaseModel):
    course_codes: List[str] = Field(description="Every course code on the handout, e.g. ['BITS F464', 'CS F464']")
    title: Optional[str]
    instructor_in_charge: Optional[str]
    evaluation: List[EvalComponent] = Field(description="Every row of the evaluation scheme table")
    has_midsem: Optional[bool] = Field(description="Is there a mid-semester exam/test? null if unclear")
    has_compre: Optional[bool] = Field(description="Is there a comprehensive / end-semester exam? null if unclear")
    has_project: Optional[bool] = Field(description="Is a project, term paper or assignment-based project graded?")
    has_quizzes: Optional[bool]
    attendance_policy: Optional[str] = Field(description="Attendance rule copied from the handout, max 40 words. null if the handout says nothing about attendance.")
    attendance_counts_for_marks: Optional[bool] = Field(description="Does attendance directly carry marks or a penalty? null if not stated")
    makeup_policy: Optional[str] = Field(description="Make-up rule copied from the handout, max 40 words. null if not stated.")
    makeup_available: Optional[bool] = Field(description="Is make-up possible for the midsem/compre at all? null if not stated")
    makeup_needs_genuine_reason: Optional[bool] = Field(description="Only for medical/genuine reasons? null if not stated")
    makeup_needs_prior_permission: Optional[bool] = Field(description="Must permission be taken from the IC BEFORE the exam? null if not stated")
    makeup_for_quizzes_or_assignments: Optional[bool] = Field(description="Is make-up given for quizzes/assignments/tutorials? false if the handout says no. null if not stated")
    topics: List[str] = Field(description="Up to 15 main topics from the course plan / syllabus, short phrases")


PROMPT = """You are extracting facts from a BITS Pilani course handout.
Fill the schema ONLY from the text below. Do not use outside knowledge.
If the handout does not state something, use null (or "not_stated").
Do not guess attendance or make-up rules: if they are not written, they are null.

HANDOUT TEXT:
{text}
"""


# ---------- steps ----------

def code_from_filename(path):
    """'057_BITS_F464.pdf' -> 'BITS F464'"""
    _, dept, num = path.stem.split("_", 2)
    return f"{dept} {num}"


def pdf_text(path):
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)[:MAX_CHARS]


class QuotaExhausted(Exception):
    """429: the free-tier quota is used up. Waiting seconds won't help; stop and rerun later."""


def ask_llm(client, models, text, tries=3):
    """Call Gemini, trying each model in the list in turn.
    503 (overloaded): retry a few times, then move to the next model.
    429 (quota) / 404 (model removed): move to the next model at once.
    If EVERY model is unusable, stop the whole run instead of failing each file."""
    from google.genai import errors, types
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Handout,
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    last, quota_hits = None, 0
    for model in models:
        for attempt in range(tries):
            try:
                r = client.models.generate_content(model=model, contents=PROMPT.format(text=text), config=config)
                return Handout.model_validate_json(r.text).model_dump(), model
            except errors.APIError as e:
                last = e
                if e.code in (404, 429):          # 404 = model removed, 429 = out of quota
                    print(f"    {model}: {e.code} {'model not available' if e.code == 404 else 'quota'}, trying next model")
                    quota_hits += 1
                    break
                if e.code in (500, 503):
                    wait = 5 * 2 ** attempt
                    print(f"    {model}: {e.code}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
    if quota_hits == len(models):
        raise QuotaExhausted(str(last))
    raise last


def validate(rec, file_code):
    """Checks done by code, not by the LLM. Returns a list of problems."""
    issues = []
    norm = lambda c: re.sub(r"\s+", "", c.upper())          # 'BITS F 429', 'CEG551' -> no spaces
    base = norm(re.sub(r"-\d$", "", file_code))             # 'BITS F101-1' -> 'BITSF101'
    if not any(norm(c).startswith(base) for c in rec["course_codes"]):
        issues.append(f"filename code {file_code} not among codes on handout {rec['course_codes']}")

    weights = [c["weight_percent"] for c in rec["evaluation"] if c["weight_percent"] is not None]
    if not rec["evaluation"]:
        issues.append("no evaluation scheme found")
    elif len(weights) < len(rec["evaluation"]):
        issues.append("some evaluation weights missing")
    elif not 95 <= sum(weights) <= 105:
        issues.append(f"evaluation weights sum to {sum(weights):.0f}, not 100")

    names = " ".join(c["component"].lower() for c in rec["evaluation"])
    mid_in_table = bool(re.search(r"mid", names))
    if rec["has_midsem"] is not None and rec["has_midsem"] != mid_in_table:
        issues.append(f"has_midsem={rec['has_midsem']} but evaluation table says {mid_in_table}")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("handout_dir")
    ap.add_argument("out_jsonl")
    ap.add_argument("--limit", type=int, default=None, help="only process N new handouts (for testing)")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-run the code checks on already saved rows (no LLM calls) and exit")
    ap.add_argument("--sleep", type=float, default=4.0, help="seconds between calls (free-tier rate limit)")
    args = ap.parse_args()

    if args.revalidate:
        p = Path(args.out_jsonl)
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        for r in rows:
            if r["extracted"] is not None:
                r["needs_verification"] = validate(r["extracted"], r["code"])
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"revalidated {len(rows)} rows, {sum(bool(r['needs_verification']) for r in rows)} flagged")
        return

    load_dotenv()
    from google import genai
    from google.genai import types
    # 90 s timeout: without it a dropped connection can hang the run forever
    client = genai.Client(http_options=types.HttpOptions(timeout=90_000))
    # first model is preferred; the rest are fallbacks when it is overloaded
    models = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3.5-flash,gemini-3.1-flash-lite").split(",")]

    out = Path(args.out_jsonl)
    done = set()
    if out.exists():
        # failed LLM calls are NOT done: they get retried on the next run
        for line in out.read_text().splitlines():
            r = json.loads(line) if line.strip() else None
            if r and (r["extracted"] is not None or "scanned" in " ".join(r["needs_verification"])):
                done.add(r["file"])
    files = sorted(p for p in Path(args.handout_dir).glob("*.pdf") if p.name not in done)
    if args.limit:
        files = files[: args.limit]
    print(f"{len(done)} already done, {len(files)} to process, models={models}")

    with out.open("a") as f:
        for i, path in enumerate(files, 1):
            file_code = code_from_filename(path)
            base = {"file": path.name, "code": file_code, "source": {"doc": f"handouts/{path.name}"}}
            text = pdf_text(path)
            if len(text.split()) < MIN_WORDS:
                rec = {**base, "extracted": None,
                       "needs_verification": ["scanned PDF with no text layer; needs OCR or manual entry"]}
            else:
                try:
                    data, used = ask_llm(client, models, text)
                    rec = {**base, "model": used, "extracted": data,
                           "needs_verification": validate(data, file_code)}
                except QuotaExhausted as e:
                    print(f"\nQuota used up (429). Stopped after {i - 1} handouts this run.")
                    print("Everything done so far is saved. Run the same command later to continue.")
                    print(str(e)[:1500])
                    return
                except Exception as e:                      # keep going; this file is retried next run
                    print(f"[{i}/{len(files)}] {file_code}  ! failed, will retry next run: {str(e)[:80]}")
                    continue
            f.write(json.dumps(rec) + "\n")
            f.flush()
            flag = "  !" if rec["needs_verification"] else ""
            print(f"[{i}/{len(files)}] {file_code}{flag} {'; '.join(rec['needs_verification'])[:100]}")
            time.sleep(args.sleep)


if __name__ == "__main__":
    main()
