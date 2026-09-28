"""Turn the student's free-text question into structured preferences.

This is the only step where the LLM reads the student's words. It does NOT
see any course data here, so it cannot invent or pick courses; it only fills
in the Preferences form below. Everything after this is done by code.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from core import llm


class Preferences(BaseModel):
    category: Optional[Literal["CDC", "GIR", "DEL", "HUEL", "OPEL"]] = Field(
        description="Requirement category the student asked for. null if not specified.")
    interests: List[str] = Field(
        description="Subject areas the student wants, e.g. ['machine learning', 'finance']. Empty if none.")
    no_midsem: Optional[bool] = Field(description="true if the student wants courses with no mid-semester exam")
    no_attendance_requirement: Optional[bool] = Field(
        description="true if the student wants no attendance requirement / no attendance marks")
    lenient_makeup: Optional[bool] = Field(description="true if the student wants a lenient make-up policy")
    prefers_project: Optional[bool] = Field(description="true if the student prefers project-based evaluation")
    avoid_quizzes: Optional[bool] = Field(description="true if the student wants no quizzes")
    no_8am: Optional[bool] = Field(description="true if the student wants no 8 AM classes")
    free_days: List[str] = Field(description="Weekdays the student wants kept free, e.g. ['Friday']. Empty if none.")
    compact_timetable: Optional[bool] = Field(
        description="true if the student wants a compact timetable / no long gaps between classes")
    count: int = Field(description="How many courses to suggest. Default 5 if not stated.")


PROMPT = """A BITS Pilani student asked a course-selection question.
Convert it into the preference form. Only fill what the student actually asked for;
leave everything else null or empty. Category meanings:
CDC = compulsory discipline core, GIR = general institutional requirement (first/second-year
foundation courses like Environmental Studies or Principles of Economics),
DEL = discipline elective, HUEL = humanities elective, OPEL = open elective (any other course).

Question: {q}
"""


def parse_query(question: str) -> Preferences:
    return llm.ask(PROMPT.format(q=question), Preferences)
