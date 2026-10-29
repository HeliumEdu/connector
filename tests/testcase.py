import json
import unittest
from datetime import date, time
from decimal import Decimal
from typing import Any

import httpx

from helium_connector.models import Category, Course, CourseGroup, Event, Homework, Meeting, Repeat, TermPlan, \
    WeeklySchedule

TOKEN = "good-token"
USER = {"id": 7, "email": "student@example.com", "settings": {"time_zone": "America/Chicago"}}
IMPORT_COUNTS = {"external_calendars": 0, "course_groups": 1, "courses": 2, "course_schedules": 3, "categories": 3,
                 "resource_groups": 0, "resources": 0, "events": 2, "homework": 3, "reminders": 0, "notes": 0}


class FakeHelium:
    """
    Helium's API, answering from canned responses and recording every request.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", "/auth/user/"): (200, USER),
            ("POST", "/importexport/import/"): (200, IMPORT_COUNTS),
            ("GET", "/planner/coursegroups/"): (200, []),
        }

    def answer(self, method: str, path: str, status: int = 200, body: Any = None) -> None:
        self.answers[(method, path)] = (status, body)

    def requested(self, method: str, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == method and request.url.path == path]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Given token not valid for any token type"})
        status, body = self.answers.get((request.method, request.url.path), (404, {"detail": "Not found."}))
        return httpx.Response(status, content=json.dumps(body).encode("utf-8") if body is not None else b"")


def uploaded_import(request: httpx.Request) -> dict[str, Any]:
    """
    The JSON file a multipart import request carries.
    """
    content = request.read().decode("utf-8")
    start = content.index("{", content.index("Content-Type: application/json"))
    end = content.rindex("}") + 1
    loaded: dict[str, Any] = json.loads(content[start:end])
    return loaded


def given_a_plan(**changes: Any) -> TermPlan:
    plan = TermPlan(
        course_group=CourseGroup(title="Fall 2026", start_date=date(2026, 8, 31), end_date=date(2026, 12, 18),
                                 no_class_dates=[date(2026, 9, 7), date(2026, 11, 26)]),
        courses=[
            Course(title="BIO 151", credits=Decimal("4.00"), room="Bagley 131", teacher_name="Dr. Smith",
                   teacher_email="smith@example.edu",
                   schedules=[WeeklySchedule(pattern="weekly", meetings=[
                       Meeting(days=["monday", "wednesday", "friday"], start_time=time(10), end_time=time(10, 50)),
                       Meeting(days=["monday"], start_time=time(14), end_time=time(16, 50))])],
                   categories=[Category(title="Homework", weight=Decimal(40)),
                               Category(title="Exams", weight=Decimal(60))],
                   homework=[Homework(title="Problem Set 1", category="homework", due_date=date(2026, 9, 14),
                                      due_time=time(23, 59)),
                             Homework(title="Final Exam", category="Exams", due_date=date(2026, 12, 14),
                                      due_time=time(8), end_time=time(10))]),
            Course(title="ENG 101", is_online=True,
                   homework=[Homework(title="Essay 1", due_date=date(2026, 10, 2))]),
        ],
        events=[Event(title="Office Hours", start_date=date(2026, 9, 1), start_time=time(15), end_time=time(16),
                      repeat=Repeat(days=["tuesday"], until=date(2026, 9, 8)))],
    )
    return plan.model_copy(update=changes)


class AsyncTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.helium = FakeHelium()
