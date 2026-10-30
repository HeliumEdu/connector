import unittest
from datetime import date
from zoneinfo import ZoneInfo

from helium_connector.reads import (
    MAX_NOTE_CHARS,
    delta_to_text,
    due_between,
    expand_events,
    shape_assignments,
    shape_classes,
    shape_grades,
    shape_needed_grade,
    shape_notes,
    shape_resources,
)

CHICAGO = ZoneInfo("America/Chicago")
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
COURSE_GROUPS = [{"id": 1, "title": "Spring 2026", "start_date": "2026-01-12", "end_date": "2026-05-08",
                  "overall_grade": 91.5},
                 {"id": 2, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18",
                  "overall_grade": -1}]
COURSES = [{"id": 10, "course_group": 2, "title": "BIO 151", "room": "Bagley 131", "is_online": False,
            "teacher_name": "Dr. Smith", "teacher_email": "", "website": None, "credits": "4.00",
            "current_grade": -1},
           {"id": 11, "course_group": 1, "title": "CHEM 101", "credits": "3.00", "current_grade": 88.0}]
SCHEDULES = [{"id": 100, "course": 10, "days_of_week": "0101010", "cycle_length": None, "is_week_based": False,
              **{f"{day}_start_time": "00:00:00" for day in ("sun", "tue", "thu", "sat")},
              **{f"{day}_end_time": "00:00:00" for day in ("sun", "tue", "thu", "sat")},
              "mon_start_time": "10:00:00", "mon_end_time": "10:50:00",
              "wed_start_time": "10:00:00", "wed_end_time": "10:50:00",
              "fri_start_time": "11:00:00", "fri_end_time": "11:50:00"}]
CATEGORIES = [{"id": 200, "course": 10, "title": "Exams", "weight": "60.00"}]


class TestShapeClasses(unittest.TestCase):
    def test_current_terms_only(self) -> None:
        # WHEN
        terms = shape_classes(COURSE_GROUPS, COURSES, SCHEDULES, CATEGORIES, date(2026, 10, 1), False)

        # THEN
        self.assertEqual(["Fall 2026"], [term["title"] for term in terms])
        bio = terms[0]["classes"][0]
        self.assertIsNone(terms[0]["overall_grade"])
        self.assertEqual(["Mon/Wed 10:00-10:50; Fri 11:00-11:50"], bio["meets"])
        self.assertEqual([{"id": 200, "title": "Exams", "weight": "60.00"}], bio["categories"])
        self.assertIsNone(bio["teacher_email"])
        self.assertIsNone(bio["current_grade"])

    def test_dates_and_grading(self) -> None:
        # GIVEN
        groups = [{**COURSE_GROUPS[1], "exceptions": "20260907,20261126"}]
        courses = [{**COURSES[0], "start_date": "2026-10-19", "end_date": "2026-12-11", "exceptions": "20261125"}]
        categories = [{"id": 201, "course": 10, "title": "Tests", "weight": "0.00"},
                      {"id": 202, "course": 10, "title": "Homework", "weight": "0.00"}]

        # WHEN
        term = shape_classes(groups, courses, [], categories, date(2026, 10, 1), False)[0]

        # THEN
        self.assertEqual(["2026-09-07", "2026-11-26"], term["no_class_dates"])
        bio = term["classes"][0]
        self.assertEqual(("2026-10-19", "2026-12-11", ["2026-11-25"]),
                         (bio["start_date"], bio["end_date"], bio["no_class_dates"]))
        self.assertEqual("points-based", bio["grading"])

    def test_past_terms(self) -> None:
        # WHEN
        terms = shape_classes(COURSE_GROUPS, COURSES, SCHEDULES, CATEGORIES, date(2026, 10, 1), True)

        # THEN
        self.assertEqual(["Spring 2026", "Fall 2026"], [term["title"] for term in terms])
        self.assertEqual(91.5, terms[0]["overall_grade"])
        self.assertEqual(88.0, terms[0]["classes"][0]["current_grade"])


class TestDescribeSchedules(unittest.TestCase):
    def test_every_pattern(self):
        # GIVEN
        off = {f"{day}_{edge}_time": "00:00:00" for day in ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
               for edge in ("start", "end")}
        schedules = [
            {**off, "id": 1, "course": 10, "days_of_week": "0010000", "is_week_based": True, "week_offset": 1,
             "anchor_date": "2026-09-02", "tue_start_time": "13:00:00", "tue_end_time": "14:00:00"},
            {**off, "id": 2, "course": 10, "days_of_week": "0000000", "cycle_length": 2, "anchor_date": "2026-08-31",
             "cycle_slots": [{"indices": [1, 2], "start_time": "08:00:00", "end_time": "09:00:00"}],
             "start_date": "2026-09-08", "end_date": "2026-10-30"},
            {**off, "id": 3, "course": 10, "days_of_week": "0000000", "cycle_length": 6, "anchor_date": "2026-08-31",
             "cycle_slots": [{"indices": [1, 3], "start_time": "09:00:00", "end_time": "09:50:00"},
                             {"indices": [5], "start_time": "13:00:00", "end_time": "14:30:00"}]},
        ]

        # WHEN
        terms = shape_classes(COURSE_GROUPS, COURSES, schedules, [], date(2026, 10, 1), False)

        # THEN
        self.assertEqual(["Week B (Week A includes 2026-09-02): Tue 13:00-14:00",
                          "A/B Day (A Day on 2026-08-31): A and B Days 08:00-09:00, 2026-09-08 to 2026-10-30",
                          "6-day cycle (Day 1 on 2026-08-31): Days 1 and 3 09:00-09:50; Day 5 13:00-14:30"],
                         terms[0]["classes"][0]["meets"])


class TestShapeAssignments(unittest.TestCase):
    def test_assignments(self) -> None:
        # GIVEN
        homework = [{"id": 1, "title": "Problem Set 1", "course": 10, "category": 200, "all_day": False,
                     "start": "2026-09-15T04:59:00Z", "completed": True, "current_grade": "18/20", "priority": 50},
                    {"id": 2, "title": "Essay", "course": 10, "category": None, "all_day": True,
                     "start": "2026-10-31T05:00:00Z", "completed": False, "current_grade": "-1/100",
                     "priority": 70}]

        # WHEN
        shaped = shape_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # THEN
        self.assertEqual({"id": 1, "title": "Problem Set 1", "class": "BIO 151", "class_id": 10, "term": "Fall 2026",
                          "category": "Exams",
                          "due": "2026-09-14T23:59:00-05:00", "end": None, "all_day": False, "completed": True,
                          "grade": "18/20", "priority": 50}, shaped[0])
        self.assertEqual(("2026-10-31", None, None), (shaped[1]["due"], shaped[1]["grade"], shaped[1]["category"]))

    def test_exam_end_time(self) -> None:
        # GIVEN
        homework = [{"id": 3, "title": "Midterm", "course": 10, "all_day": False, "show_end_time": True,
                     "start": "2026-10-14T15:00:00Z", "end": "2026-10-14T16:50:00Z", "current_grade": "-1/100"}]

        # WHEN
        shaped = shape_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # THEN
        self.assertEqual(("2026-10-14T10:00:00-05:00", "2026-10-14T11:50:00-05:00"),
                         (shaped[0]["due"], shaped[0]["end"]))


class TestDueBetween(unittest.TestCase):
    def test_all_day_items_stay_on_their_local_day(self) -> None:
        # GIVEN
        homework = [{"id": day, "title": f"Quiz {day}", "course": 10, "all_day": True,
                     "start": f"2026-09-{day:02d}T05:00:00Z"} for day in (17, 18, 19)]
        shaped = shape_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # WHEN
        kept = due_between(shaped, date(2026, 9, 18), date(2026, 9, 18))

        # THEN
        self.assertEqual(["Quiz 18"], [item["title"] for item in kept])

    def test_positive_offset_zone(self) -> None:
        # GIVEN
        homework = [{"id": 1, "title": "Essay", "course": 10, "all_day": True, "start": "2026-09-16T22:00:00Z"},
                    {"id": 2, "title": "Lab", "course": 10, "all_day": False, "start": "2026-09-17T21:59:00Z"}]
        shaped = shape_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, AMSTERDAM)

        # WHEN
        kept = due_between(shaped, date(2026, 9, 17), date(2026, 9, 17))

        # THEN
        self.assertEqual(["Essay", "Lab"], [item["title"] for item in kept])

    def test_across_the_end_of_daylight_saving(self) -> None:
        # GIVEN
        homework = [{"id": 1, "title": "Before", "course": 10, "all_day": True, "start": "2026-10-31T05:00:00Z"},
                    {"id": 2, "title": "On", "course": 10, "all_day": True, "start": "2026-11-01T05:00:00Z"},
                    {"id": 3, "title": "After", "course": 10, "all_day": True, "start": "2026-11-02T06:00:00Z"}]
        shaped = shape_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # WHEN
        kept = due_between(shaped, date(2026, 11, 1), date(2026, 11, 1))

        # THEN
        self.assertEqual(["On"], [item["title"] for item in kept])


class TestShapeResources(unittest.TestCase):
    def test_filtered_to_a_class(self) -> None:
        # GIVEN
        groups = [{"id": 1, "title": "Fall Textbooks"}, {"id": 2, "title": "Lab Gear"}]
        resources = [{"id": 5, "title": "Campbell Biology", "status": 7, "condition": 8, "resource_group": 1,
                      "courses": [10]},
                     {"id": 6, "title": "Goggles", "status": 99, "resource_group": 2, "courses": [11]}]

        # WHEN
        everything = shape_resources(groups, resources, COURSES)
        for_bio = shape_resources(groups, resources, COURSES, class_id=10)

        # THEN
        self.assertEqual(["Fall Textbooks", "Lab Gear"], [group["title"] for group in everything])
        self.assertIsNone(everything[1]["resources"][0]["status"])
        self.assertEqual([{"id": 11, "title": "CHEM 101"}], everything[1]["resources"][0]["classes"])
        self.assertEqual([("Fall Textbooks", "Digital", "Digital", ["BIO 151"])],
                         [(group["title"], resource["status"], resource["condition"],
                           [course["title"] for course in resource["classes"]])
                          for group in for_bio for resource in group["resources"]])


class TestExpandEvents(unittest.TestCase):
    def test_single_event(self) -> None:
        # GIVEN
        events = [{"id": 1, "title": "Advising", "start": "2026-09-02T20:00:00Z", "end": "2026-09-02T20:30:00Z",
                   "all_day": False, "recurrence_rule": None}]

        # WHEN
        expanded = expand_events(events, date(2026, 9, 1), date(2026, 9, 7), CHICAGO)

        # THEN
        self.assertEqual([{"id": 1, "title": "Advising", "start": "2026-09-02T15:00:00-05:00",
                           "end": "2026-09-02T15:30:00-05:00", "all_day": False, "location": None,
                           "priority": None, "repeats": False}], expanded)

    def test_weekly_series_keeps_its_local_time_across_dst(self) -> None:
        # GIVEN
        events = [{"id": 5, "title": "Office Hours", "start": "2026-10-20T20:00:00Z", "end": "2026-10-20T21:00:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231T000000Z",
                   "exception_dates": ["2026-10-27T20:00:00Z"], "location": "Hall 2"}]

        # WHEN
        expanded = expand_events(events, date(2026, 10, 19), date(2026, 11, 10), CHICAGO)

        # THEN
        self.assertEqual(["2026-10-20T15:00:00-05:00", "2026-11-03T15:00:00-06:00", "2026-11-10T15:00:00-06:00"],
                         [occurrence["start"] for occurrence in expanded])
        self.assertTrue(all(occurrence["repeats"] for occurrence in expanded))

    def test_all_day_event(self) -> None:
        # GIVEN
        events = [{"id": 3, "title": "No classes", "start": "2026-11-26T06:00:00Z", "end": "2026-11-27T06:00:00Z",
                   "all_day": True}]

        # WHEN
        expanded = expand_events(events, date(2026, 11, 26), date(2026, 11, 26), CHICAGO)

        # THEN
        self.assertEqual([("2026-11-26", None, True)],
                         [(event["start"], event["end"], event["all_day"]) for event in expanded])

    def test_outside_the_window(self) -> None:
        # GIVEN
        events = [{"id": 1, "title": "Advising", "start": "2026-09-20T20:00:00Z", "end": "2026-09-20T20:30:00Z"}]

        # WHEN
        expanded = expand_events(events, date(2026, 9, 1), date(2026, 9, 7), CHICAGO)

        # THEN
        self.assertEqual([], expanded)


class TestShapeGrades(unittest.TestCase):
    def test_grades(self) -> None:
        # GIVEN
        exams = {"id": 3, "title": "Exams", "weight": 60, "overall_grade": -1, "trend": -0.0004, "num_homework": 2,
                 "num_homework_graded": 0}
        bio = {"id": 10, "title": "BIO 151", "overall_grade": -1, "trend": None, "has_weighted_grading": True,
               "num_homework": 4, "num_homework_graded": 0, "points_earned": 0, "points_possible": 0,
               "categories": [exams]}
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 90.123, "trend": 0.0021,
                                     "courses": [bio]},
                                    {"id": 1, "title": "Spring 2026", "overall_grade": 88, "courses": []}]}

        # WHEN
        shaped = shape_grades(grades, {2})

        # THEN
        self.assertEqual(1, len(shaped))
        self.assertEqual((2, 90.12, "up"), (shaped[0]["id"], shaped[0]["overall_grade"], shaped[0]["trend"]))
        bio = shaped[0]["classes"][0]
        self.assertEqual((10, None, None), (bio["id"], bio["overall_grade"], bio["trend"]))
        self.assertTrue(bio["weighted"])
        self.assertEqual({"id": 3, "title": "Exams", "weight": 60, "average_grade": None, "trend": "down",
                          "graded_assignments": 0, "ungraded_assignments": 2}, bio["categories"][0])

    def test_needed_grade_rounds_ties_as_the_app_does(self) -> None:
        # GIVEN
        shown_in_the_app = {76.25: 76.3, 83.35: 83.3, 2.675: 2.7, 100.05: 100.0, 99.95: 100.0, -0.35: -0.4}

        # WHEN
        shaped = {value: shape_needed_grade({"state": "achievable", "needed_grade": value})["needed_grade"]
                  for value in shown_in_the_app}

        # THEN
        self.assertEqual(shown_in_the_app, shaped)

    def test_steady_trend(self) -> None:
        # GIVEN
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 90, "trend": 0,
                                     "courses": []}]}

        # WHEN
        shaped = shape_grades(grades)

        # THEN
        self.assertEqual("steady", shaped[0]["trend"])

    def test_needed_grade_rounds_as_the_app_shows_it(self) -> None:
        # GIVEN
        needed = {"state": "achievable", "needed_grade": 83.33333, "weighted": True,
                  "category": {"id": 3, "title": "Exams"},
                  "assignment": {"id": 9, "title": "Final", "points_possible": 100.0, "needed_points": 83.33333},
                  "total_weight_accounted_for": None}

        # WHEN
        shaped = shape_needed_grade(needed)

        # THEN
        self.assertEqual({"outcome": "achievable", "needed_grade": 83.3, "category": "Exams",
                          "assignment": {"title": "Final", "points_possible": 100.0, "needed_points": 83.3},
                          "total_weight": None}, shaped)


class TestNotes(unittest.TestCase):
    def test_delta_to_text(self) -> None:
        # WHEN / THEN
        self.assertEqual("Mitosis\nProphase first", delta_to_text(
            {"ops": [{"insert": "Mitosis\n", "attributes": {"header": 1}}, {"insert": {"image": "x.png"}},
                     {"insert": "Prophase first\n"}]}))
        self.assertEqual("plain", delta_to_text('{"ops": [{"insert": "plain"}]}'))
        self.assertEqual("", delta_to_text(None))

    def test_notes_most_recent_first(self) -> None:
        # GIVEN
        notes = [{"id": 1, "title": "Old", "content": {"ops": [{"insert": "a"}]},
                  "updated_at": "2026-09-01T00:00:00Z"},
                 {"id": 2, "title": "New", "content": {"ops": [{"insert": "b" * (MAX_NOTE_CHARS + 50)}]},
                  "updated_at": "2026-09-05T15:00:00Z", "linked_entity_title": "Lab 1",
                  "linked_entity_type": "homework", "course_id": 10}]

        # WHEN
        shaped = shape_notes(notes, [{"id": 10, "title": "BIO 151"}], CHICAGO, 1)

        # THEN
        self.assertEqual(1, len(shaped))
        self.assertEqual(("New", "Lab 1", "homework", "BIO 151"),
                         (shaped[0]["title"], shaped[0]["linked_to"], shaped[0]["linked_type"], shaped[0]["class"]))
        self.assertEqual("2026-09-05T10:00:00-05:00", shaped[0]["updated"])
        self.assertEqual(MAX_NOTE_CHARS, len(shaped[0]["text"]))
