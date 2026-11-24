import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from helium_connector.platform_enums import RECURRENCE_HORIZON_YEARS
from helium_connector.serializers import (
    delta_to_text,
    due_between,
    expand_events,
    serialize_assignments,
    serialize_classes,
    serialize_grades,
    serialize_needed_grade,
    serialize_notes,
    serialize_resources,
)
from helium_connector.settings import MAX_NOTE_CHARS

CHICAGO = ZoneInfo("America/Chicago")
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
COURSE_GROUPS = [{"id": 1, "title": "Spring 2026", "start_date": "2026-01-12", "end_date": "2026-05-08",
                  "exceptions": "", "overall_grade": 91.5},
                 {"id": 2, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18",
                  "exceptions": "", "overall_grade": -1}]
COURSES = [{"id": 10, "course_group": 2, "title": "BIO 151", "room": "Bagley 131", "is_online": False,
            "teacher_name": "Dr. Smith", "teacher_email": "", "website": None, "credits": "4.00", "exceptions": "",
            "current_grade": -1},
           {"id": 11, "course_group": 1, "title": "CHEM 101", "credits": "3.00", "exceptions": "",
            "current_grade": 88.0}]
SCHEDULES = [{"id": 100, "course": 10, "days_of_week": "0101010", "cycle_length": None, "is_week_based": False,
              "exceptions": "",
              **{f"{day}_start_time": "00:00:00" for day in ("sun", "tue", "thu", "sat")},
              **{f"{day}_end_time": "00:00:00" for day in ("sun", "tue", "thu", "sat")},
              "mon_start_time": "10:00:00", "mon_end_time": "10:50:00",
              "wed_start_time": "10:00:00", "wed_end_time": "10:50:00",
              "fri_start_time": "11:00:00", "fri_end_time": "11:50:00"}]
CATEGORIES = [{"id": 200, "course": 10, "title": "Exams", "weight": "60.00"}]


class TestSerializeClasses(unittest.TestCase):
    def test_current_terms_only(self) -> None:
        # WHEN
        terms = serialize_classes(COURSE_GROUPS, COURSES, SCHEDULES, CATEGORIES, date(2026, 10, 1), False)

        # THEN
        self.assertEqual(["Fall 2026"], [term["title"] for term in terms])
        bio = terms[0]["classes"][0]
        self.assertIsNone(terms[0]["overall_grade"])
        self.assertEqual(["Mon/Wed 10:00-10:50; Fri 11:00-11:50"], bio["meets"])
        self.assertEqual([{"id": 200, "title": "Exams", "weight": "60.00"}], bio["categories"])
        self.assertIsNone(bio["teacher_email"])
        self.assertIsNone(bio["current_grade"])

    def test_grade_scales(self) -> None:
        # GIVEN
        plus_minus = [{"min_grade": 0.0, "grade_point": 0.0, "label": "F"},
                      {"min_grade": 90.0, "grade_point": 3.7, "label": "A-"}]
        groups = [{**COURSE_GROUPS[1], "id": 2, "grade_scale": plus_minus, "grade_scale_template": 2},
                  {**COURSE_GROUPS[1], "id": 3, "grade_scale": plus_minus, "grade_scale_template": None},
                  {**COURSE_GROUPS[1], "id": 4, "grade_scale": [], "grade_scale_template": 0}]
        courses = [{**COURSES[0], "course_group": 2, "grade_scale_template": 1},
                   {**COURSES[0], "id": 12, "course_group": 2, "grade_scale_template": None}]

        # WHEN
        terms = serialize_classes(groups, courses, [], [], date(2026, 10, 1), False)

        # THEN
        self.assertEqual(["4.0 with +/-", "custom", None], [term["grade_scale"] for term in terms])
        self.assertEqual(["4.0", None], [course["grade_scale"] for course in terms[0]["classes"]])

    def test_dates_and_grading(self) -> None:
        # GIVEN
        groups = [{**COURSE_GROUPS[1], "exceptions": "20260907,20261126"}]
        courses = [{**COURSES[0], "start_date": "2026-10-19", "end_date": "2026-12-11", "exceptions": "20261125"}]
        categories = [{"id": 201, "course": 10, "title": "Tests", "weight": "0.00"},
                      {"id": 202, "course": 10, "title": "Homework", "weight": "0.00"}]

        # WHEN
        term = serialize_classes(groups, courses, [], categories, date(2026, 10, 1), False)[0]

        # THEN
        self.assertEqual(["2026-09-07", "2026-11-26"], term["no_class_dates"])
        bio = term["classes"][0]
        self.assertEqual(("2026-10-19", "2026-12-11", ["2026-11-25"]),
                         (bio["start_date"], bio["end_date"], bio["no_class_dates"]))
        self.assertEqual("points-based", bio["grading"])

    def test_a_schedules_own_cancellations_are_in_its_meeting_times(self) -> None:
        # GIVEN
        schedules = [{**SCHEDULES[0], "end_date": "2026-12-11", "exceptions": "20261120,20261202"}]

        # WHEN
        terms = serialize_classes(COURSE_GROUPS, COURSES, schedules, CATEGORIES, date(2026, 10, 1), False)

        # THEN
        self.assertEqual(["Mon/Wed 10:00-10:50; Fri 11:00-11:50, until 2026-12-11, except 2026-11-20, 2026-12-02"],
                         terms[0]["classes"][0]["meets"])

    def test_past_terms(self) -> None:
        # WHEN
        terms = serialize_classes(COURSE_GROUPS, COURSES, SCHEDULES, CATEGORIES, date(2026, 10, 1), True)

        # THEN
        self.assertEqual(["Spring 2026", "Fall 2026"], [term["title"] for term in terms])
        self.assertEqual(91.5, terms[0]["overall_grade"])
        self.assertEqual(88.0, terms[0]["classes"][0]["current_grade"])


class TestDescribeSchedules(unittest.TestCase):
    def test_every_pattern(self):
        # GIVEN
        off = {"exceptions": "", **{f"{day}_{edge}_time": "00:00:00"
                                    for day in ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
                                    for edge in ("start", "end")}}
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
        terms = serialize_classes(COURSE_GROUPS, COURSES, schedules, [], date(2026, 10, 1), False)

        # THEN
        self.assertEqual(["Week B (Week A includes 2026-09-02): Tue 13:00-14:00",
                          "A/B Day (A Day on 2026-08-31): A and B Days 08:00-09:00, 2026-09-08 to 2026-10-30",
                          "6-day cycle (Day 1 on 2026-08-31): Days 1 and 3 09:00-09:50; Day 5 13:00-14:30"],
                         terms[0]["classes"][0]["meets"])


class TestSerializeAssignments(unittest.TestCase):
    def test_assignments(self) -> None:
        # GIVEN
        homework = [{"id": 1, "title": "Problem Set 1", "course": 10, "category": 200, "all_day": False,
                     "start": "2026-09-15T04:59:00Z", "completed": True, "current_grade": "18/20", "priority": 50},
                    {"id": 2, "title": "Essay", "course": 10, "category": None, "all_day": True,
                     "start": "2026-10-31T05:00:00Z", "completed": False, "current_grade": "-1/100",
                     "priority": 70}]

        # WHEN
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # THEN
        self.assertEqual({"id": 1, "title": "Problem Set 1", "class": "BIO 151", "class_id": 10, "term": "Fall 2026",
                          "category": "Exams",
                          "due": "2026-09-14T23:59:00-05:00", "end": None, "all_day": False, "completed": True,
                          "grade": "18/20", "priority": 50, "repeats": False}, serialized[0])
        self.assertEqual(("2026-10-31", None, None),
                         (serialized[1]["due"], serialized[1]["grade"], serialized[1]["category"]))

    def test_exam_end_time(self) -> None:
        # GIVEN
        homework = [{"id": 3, "title": "Midterm", "course": 10, "all_day": False, "show_end_time": True,
                     "start": "2026-10-14T15:00:00Z", "end": "2026-10-14T16:50:00Z", "current_grade": "-1/100"}]

        # WHEN
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # THEN
        self.assertEqual(("2026-10-14T10:00:00-05:00", "2026-10-14T11:50:00-05:00"),
                         (serialized[0]["due"], serialized[0]["end"]))

    def test_repeating_assignment_is_listed_once_per_occurrence_in_local_time(self) -> None:
        # GIVEN
        homework = [{"id": 4, "title": "Reading", "course": 10, "all_day": False, "show_end_time": True,
                     "start": "2026-10-23T21:00:00Z", "end": "2026-10-23T22:00:00Z", "current_grade": "-1/100",
                     "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR;UNTIL=20261114T055959Z",
                     "exception_dates": ["2026-10-30T21:00:00Z"]}]

        # WHEN
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # THEN
        self.assertEqual([("2026-10-23T16:00:00-05:00", "2026-10-23T17:00:00-05:00"),
                          ("2026-11-06T16:00:00-06:00", "2026-11-06T17:00:00-06:00"),
                          ("2026-11-13T16:00:00-06:00", "2026-11-13T17:00:00-06:00")],
                         [(item["due"], item["end"]) for item in serialized])
        self.assertTrue(all(item["id"] == 4 and item["repeats"] and not item["completed"] for item in serialized))

    def test_endless_repeating_assignment_stops_at_the_horizon(self) -> None:
        # GIVEN
        begins = datetime.now(timezone.utc) - timedelta(days=1)
        homework = [{"id": 4, "title": "Reflection", "course": 10, "all_day": False,
                     "start": begins.isoformat(), "end": begins.isoformat(), "recurrence_rule": "FREQ=YEARLY"}]

        # WHEN
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # THEN
        self.assertEqual(RECURRENCE_HORIZON_YEARS + 1, len(serialized))


class TestDueBetween(unittest.TestCase):
    def test_all_day_items_stay_on_their_local_day(self) -> None:
        # GIVEN
        homework = [{"id": day, "title": f"Quiz {day}", "course": 10, "all_day": True,
                     "start": f"2026-09-{day:02d}T05:00:00Z"} for day in (17, 18, 19)]
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # WHEN
        kept = due_between(serialized, date(2026, 9, 18), date(2026, 9, 18))

        # THEN
        self.assertEqual(["Quiz 18"], [item["title"] for item in kept])

    def test_positive_offset_zone(self) -> None:
        # GIVEN
        homework = [{"id": 1, "title": "Essay", "course": 10, "all_day": True, "start": "2026-09-16T22:00:00Z"},
                    {"id": 2, "title": "Lab", "course": 10, "all_day": False, "start": "2026-09-17T21:59:00Z"}]
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, AMSTERDAM)

        # WHEN
        kept = due_between(serialized, date(2026, 9, 17), date(2026, 9, 17))

        # THEN
        self.assertEqual(["Essay", "Lab"], [item["title"] for item in kept])

    def test_across_the_end_of_daylight_saving(self) -> None:
        # GIVEN
        homework = [{"id": 1, "title": "Before", "course": 10, "all_day": True, "start": "2026-10-31T05:00:00Z"},
                    {"id": 2, "title": "On", "course": 10, "all_day": True, "start": "2026-11-01T05:00:00Z"},
                    {"id": 3, "title": "After", "course": 10, "all_day": True, "start": "2026-11-02T06:00:00Z"}]
        serialized = serialize_assignments(homework, COURSES, CATEGORIES, COURSE_GROUPS, CHICAGO)

        # WHEN
        kept = due_between(serialized, date(2026, 11, 1), date(2026, 11, 1))

        # THEN
        self.assertEqual(["On"], [item["title"] for item in kept])


class TestSerializeResources(unittest.TestCase):
    def test_filtered_to_a_class(self) -> None:
        # GIVEN
        groups = [{"id": 1, "title": "Fall Textbooks"}, {"id": 2, "title": "Lab Gear"}]
        resources = [{"id": 5, "title": "Campbell Biology", "status": 7, "condition": 8, "resource_group": 1,
                      "courses": [10]},
                     {"id": 6, "title": "Goggles", "status": 99, "resource_group": 2, "courses": [11]}]

        # WHEN
        everything = serialize_resources(groups, resources, COURSES, None)
        for_bio = serialize_resources(groups, resources, COURSES, 10)

        # THEN
        self.assertEqual(["Fall Textbooks", "Lab Gear"], [group["title"] for group in everything])
        self.assertIsNone(everything[1]["resources"][0]["status"])
        self.assertEqual([{"id": 11, "title": "CHEM 101"}], everything[1]["resources"][0]["classes"])
        self.assertEqual([("Fall Textbooks", "Digital", "Digital", ["BIO 151"])],
                         [(group["title"], resource["status"], resource["condition"],
                           [course["title"] for course in resource["classes"]])
                          for group in for_bio for resource in group["resources"]])


class TestExpandEvents(unittest.TestCase):
    def test_window_past_the_horizon_lists_nothing(self) -> None:
        # GIVEN
        events = [{"id": 6, "title": "Standup", "start": "2026-09-01T14:00:00Z", "end": "2026-09-01T14:15:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=DAILY"}]

        # WHEN
        expanded = expand_events(events, date(9000, 1, 1), date(9000, 1, 7), CHICAGO)

        # THEN
        self.assertEqual([], expanded)

    def test_single_event(self) -> None:
        # GIVEN
        events = [{"id": 1, "title": "Advising", "start": "2026-09-02T20:00:00Z", "end": "2026-09-02T20:30:00Z",
                   "all_day": False, "recurrence_rule": None}]

        # WHEN
        expanded = expand_events(events, date(2026, 9, 1), date(2026, 9, 7), CHICAGO)

        # THEN
        self.assertEqual([{"id": 1, "title": "Advising", "start": "2026-09-02T15:00:00-05:00",
                           "end": "2026-09-02T15:30:00-05:00", "all_day": False,
                           "priority": None, "repeats": False}], expanded)

    def test_weekly_series_keeps_its_local_time_across_dst(self) -> None:
        # GIVEN
        events = [{"id": 5, "title": "Office Hours", "start": "2026-10-20T20:00:00Z", "end": "2026-10-20T21:00:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231T000000Z",
                   "exception_dates": ["2026-10-27T20:00:00Z"]}]

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

    def test_all_day_series_skips_an_occurrence_excluded_at_its_local_midnight(self) -> None:
        for time_zone, start, end, exception, expected in (
                (AMSTERDAM, "2026-10-18T22:00:00Z", "2026-10-19T22:00:00Z", "2026-10-25T23:00:00Z",
                 ["2026-10-19", "2026-11-02", "2026-11-09"]),
                (LOS_ANGELES, "2026-10-19T07:00:00Z", "2026-10-20T07:00:00Z", "2026-11-02T08:00:00Z",
                 ["2026-10-19", "2026-10-26", "2026-11-09"])):
            with self.subTest(time_zone=time_zone.key):
                # GIVEN
                events = [{"id": 7, "title": "Lab day", "start": start, "end": end, "all_day": True,
                           "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO", "exception_dates": [exception]}]

                # WHEN
                expanded = expand_events(events, date(2026, 10, 19), date(2026, 11, 9), time_zone)

                # THEN
                self.assertEqual(expected, [occurrence["start"] for occurrence in expanded])
                self.assertTrue(all(occurrence["all_day"] and occurrence["end"] is None for occurrence in expanded))

    def test_weekly_series_keeps_its_local_time_across_dst_east_and_west_of_utc(self) -> None:
        for time_zone, start, end, expected in (
                (AMSTERDAM, "2026-10-21T07:00:00Z", "2026-10-21T08:00:00Z",
                 ["2026-10-21T09:00:00+02:00", "2026-10-28T09:00:00+01:00", "2026-11-04T09:00:00+01:00"]),
                (LOS_ANGELES, "2026-10-21T16:00:00Z", "2026-10-21T17:00:00Z",
                 ["2026-10-21T09:00:00-07:00", "2026-10-28T09:00:00-07:00", "2026-11-04T09:00:00-08:00"])):
            with self.subTest(time_zone=time_zone.key):
                # GIVEN
                events = [{"id": 8, "title": "Study group", "start": start, "end": end, "all_day": False,
                           "recurrence_rule": "FREQ=WEEKLY;BYDAY=WE"}]

                # WHEN
                expanded = expand_events(events, date(2026, 10, 19), date(2026, 11, 8), time_zone)

                # THEN
                self.assertEqual(expected, [occurrence["start"] for occurrence in expanded])
                self.assertEqual([occurrence["start"].replace("T09:", "T10:") for occurrence in expanded],
                                 [occurrence["end"] for occurrence in expanded])

    def test_series_that_never_produces_a_date_expands_to_nothing(self) -> None:
        # GIVEN
        events = [{"id": 12, "title": "Never", "start": "2026-10-19T07:00:00Z", "end": "2026-10-19T08:00:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30;UNTIL=20271231T000000Z"}]

        # WHEN
        expanded = expand_events(events, date(2026, 10, 19), date(2026, 11, 30), LOS_ANGELES)

        # THEN
        self.assertEqual([], expanded)

    def test_series_ending_at_the_end_of_a_local_day_includes_that_day_only(self) -> None:
        # GIVEN
        events = [{"id": 9, "title": "Night lab", "start": "2026-10-20T04:00:00Z", "end": "2026-10-20T05:00:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO;UNTIL=20261110T075959Z"}]

        # WHEN
        expanded = expand_events(events, date(2026, 10, 19), date(2026, 11, 30), LOS_ANGELES)

        # THEN
        self.assertEqual(["2026-10-19T21:00:00-07:00", "2026-10-26T21:00:00-07:00", "2026-11-02T21:00:00-08:00",
                          "2026-11-09T21:00:00-08:00"], [occurrence["start"] for occurrence in expanded])

    def test_occurrence_split_from_its_series_appears_once_on_its_own(self) -> None:
        # GIVEN
        events = [{"id": 10, "title": "Office hours", "start": "2026-10-19T07:00:00Z", "end": "2026-10-19T08:00:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
                   "exception_dates": ["2026-11-02T08:00:00Z"]},
                  {"id": 11, "title": "Office hours (moved)", "start": "2026-11-03T09:00:00Z",
                   "end": "2026-11-03T10:00:00Z", "all_day": False, "recurrence_rule": None,
                   "exception_dates": None}]

        # WHEN
        expanded = expand_events(events, date(2026, 10, 26), date(2026, 11, 9), AMSTERDAM)

        # THEN
        self.assertEqual([(10, "2026-10-26T09:00:00+01:00", True), (11, "2026-11-03T10:00:00+01:00", False),
                          (10, "2026-11-09T09:00:00+01:00", True)],
                         [(occurrence["id"], occurrence["start"], occurrence["repeats"]) for occurrence in expanded])

    def test_monthly_series_on_the_last_day_lands_on_each_months_last_day(self) -> None:
        for start, end, window_end, expected in (
                ("2027-01-31T08:00:00Z", "2027-01-31T09:00:00Z", date(2027, 4, 30),
                 ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30"]),
                ("2028-01-31T08:00:00Z", "2028-01-31T09:00:00Z", date(2028, 4, 30),
                 ["2028-01-31", "2028-02-29", "2028-03-31", "2028-04-30"])):
            with self.subTest(start=start):
                # GIVEN
                events = [{"id": 12, "title": "Rent", "start": start, "end": end, "all_day": False,
                           "recurrence_rule": "FREQ=MONTHLY;BYMONTHDAY=-1"}]

                # WHEN
                expanded = expand_events(events, date(int(start[:4]), 1, 1), window_end, AMSTERDAM)

                # THEN
                self.assertEqual(expected, [occurrence["start"][:10] for occurrence in expanded])
                self.assertEqual({"T09:00:00"}, {occurrence["start"][10:19] for occurrence in expanded})

    def test_occurrence_starting_before_the_window_is_listed_while_it_runs_into_it(self) -> None:
        # GIVEN
        events = [{"id": 13, "title": "Late shift", "start": "2026-08-31T04:00:00Z", "end": "2026-08-31T06:00:00Z",
                   "all_day": False, "recurrence_rule": "FREQ=WEEKLY;BYDAY=SU"}]

        # WHEN
        expanded = expand_events(events, date(2026, 9, 7), date(2026, 9, 13), CHICAGO)

        # THEN
        self.assertEqual(["2026-09-06T23:00:00-05:00", "2026-09-13T23:00:00-05:00"],
                         [occurrence["start"] for occurrence in expanded])

    def test_outside_the_window(self) -> None:
        # GIVEN
        events = [{"id": 1, "title": "Advising", "start": "2026-09-20T20:00:00Z", "end": "2026-09-20T20:30:00Z"}]

        # WHEN
        expanded = expand_events(events, date(2026, 9, 1), date(2026, 9, 7), CHICAGO)

        # THEN
        self.assertEqual([], expanded)


class TestSerializeGrades(unittest.TestCase):
    def test_grades(self) -> None:
        # GIVEN
        exams = {"id": 3, "title": "Exams", "weight": 60, "overall_grade": -1, "trend": -0.0004, "num_homework": 2,
                 "num_homework_graded": 0}
        bio = {"id": 10, "title": "BIO 151", "overall_grade": -1, "trend": None, "has_weighted_grading": True,
               "num_homework": 4, "num_homework_graded": 0, "points_earned": 0, "points_possible": 0,
               "categories": [exams]}
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 90.123, "trend": 0.0021,
                                     "grade_scale": [], "courses": [bio]},
                                    {"id": 1, "title": "Spring 2026", "overall_grade": 88, "grade_scale": [],
                                     "courses": []}]}

        # WHEN
        serialized = serialize_grades(grades, {2})["terms"]

        # THEN
        self.assertEqual(1, len(serialized))
        self.assertEqual((2, 90.12, "up"),
                         (serialized[0]["id"], serialized[0]["overall_grade"], serialized[0]["trend"]))
        bio = serialized[0]["classes"][0]
        self.assertEqual((10, None, None), (bio["id"], bio["overall_grade"], bio["trend"]))
        self.assertTrue(bio["weighted"])
        self.assertEqual({"id": 3, "title": "Exams", "weight": 60, "average_grade": None, "trend": "down",
                          "graded_assignments": 0, "ungraded_assignments": 2}, bio["categories"][0])

    def test_gpa(self) -> None:
        # GIVEN
        bio = {"id": 10, "title": "BIO 151", "overall_grade": 86.5, "grade_point": 3.25, "credits": "4.00",
               "categories": []}
        chem = {"id": 11, "title": "CHEM 101", "overall_grade": 81.0, "grade_point": 3.0, "credits": "4.00",
                "categories": []}
        art = {"id": 12, "title": "ART 101", "overall_grade": -1, "grade_point": -1, "credits": "0.00",
               "categories": []}
        scale = [{"min_grade": 0.0, "grade_point": 0.0, "label": ""},
                 {"min_grade": 80.0, "grade_point": 3.0, "label": "B"},
                 {"min_grade": 85.0, "grade_point": 3.25, "label": "B+"}]
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 83.75, "gpa": 3.125,
                                     "in_cumulative_gpa": True, "grade_scale": scale, "courses": [bio, chem, art]}],
                  "cumulative_gpa": 3.33333}

        # WHEN
        serialized = serialize_grades(grades, {2})

        # THEN
        term = serialized["terms"][0]
        self.assertEqual((3.13, True), (term["gpa"], term["counts_toward_cumulative_gpa"]))
        self.assertEqual([None, "B", "B+"], [band["label"] for band in term["grade_scale"]])
        self.assertEqual([(3.25, "4.00"), (3.0, "4.00"), (None, "0.00")],
                         [(course["grade_point"], course["credits"]) for course in term["classes"]])
        self.assertEqual(3.33, serialized["cumulative_gpa"])

    def test_class_with_its_own_grade_scale(self) -> None:
        # GIVEN
        whole_letter = [{"min_grade": 0.0, "grade_point": 0.0, "label": "F"},
                        {"min_grade": 90.0, "grade_point": 4.0, "label": "A"}]
        plus_minus = [{"min_grade": 0.0, "grade_point": 0.0, "label": "F"},
                      {"min_grade": 90.0, "grade_point": 3.7, "label": "A-"}]
        own = {"id": 10, "title": "BIO 151", "overall_grade": 91, "grade_point": 4.0, "credits": "4.00",
               "grade_scale_template": 1, "grade_scale": whole_letter, "categories": []}
        term_scale = {"id": 11, "title": "CHEM 101", "overall_grade": 91, "grade_point": 3.7, "credits": "4.00",
                      "grade_scale_template": None, "grade_scale": plus_minus, "categories": []}
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 91, "gpa": 3.85,
                                     "in_cumulative_gpa": True, "grade_scale": plus_minus,
                                     "courses": [own, term_scale]}],
                  "cumulative_gpa": 3.85}

        # WHEN
        classes = serialize_grades(grades, {2})["terms"][0]["classes"]

        # THEN
        self.assertEqual(["F", "A"], [band["label"] for band in classes[0]["grade_scale"]])
        self.assertIsNone(classes[1]["grade_scale"], "a class on its term's scale doesn't repeat it")

    def test_term_on_another_scale_doesnt_count_toward_cumulative(self) -> None:
        # GIVEN
        grades = {"course_groups": [{"id": 2, "title": "Abroad", "overall_grade": 88, "gpa": 1.7,
                                     "in_cumulative_gpa": False, "grade_scale": [], "courses": []}],
                  "cumulative_gpa": 3.5}

        # WHEN
        serialized = serialize_grades(grades, {2})

        # THEN
        self.assertFalse(serialized["terms"][0]["counts_toward_cumulative_gpa"])

    def test_term_without_a_grade_scale_has_no_gpa(self) -> None:
        # GIVEN
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 91, "gpa": -1,
                                     "in_cumulative_gpa": False, "grade_scale": [], "courses": []}],
                  "cumulative_gpa": -1}

        # WHEN
        serialized = serialize_grades(grades, {2})

        # THEN
        self.assertEqual(([], None), (serialized["terms"][0]["grade_scale"], serialized["terms"][0]["gpa"]))
        self.assertIsNone(serialized["cumulative_gpa"])

    def test_grades_round_ties_as_the_app_does(self) -> None:
        # GIVEN
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 90.125, "gpa": 3.125,
                                     "grade_scale": [], "courses": []}]}

        # WHEN
        term = serialize_grades(grades, {2})["terms"][0]

        # THEN
        self.assertEqual((90.13, 3.13), (term["overall_grade"], term["gpa"]))

    def test_needed_grade_rounds_ties_as_the_app_does(self) -> None:
        # GIVEN
        shown_in_the_app = {76.25: 76.3, 83.35: 83.3, 2.675: 2.7, 100.05: 100.0, 99.95: 100.0, -0.35: -0.4}

        # WHEN
        serialized = {value: serialize_needed_grade({"state": "achievable", "needed_grade": value})["needed_grade"]
                      for value in shown_in_the_app}

        # THEN
        self.assertEqual(shown_in_the_app, serialized)

    def test_steady_trend(self) -> None:
        # GIVEN
        grades = {"course_groups": [{"id": 2, "title": "Fall 2026", "overall_grade": 90, "trend": 0,
                                     "grade_scale": [], "courses": []}]}

        # WHEN
        serialized = serialize_grades(grades, {2})

        # THEN
        self.assertEqual("steady", serialized["terms"][0]["trend"])

    def test_needed_grade_rounds_as_the_app_shows_it(self) -> None:
        # GIVEN
        needed = {"state": "achievable", "needed_grade": 83.33333, "weighted": True,
                  "category": {"id": 3, "title": "Exams"},
                  "assignment": {"id": 9, "title": "Final", "points_possible": 100.0, "needed_points": 83.33333}}

        # WHEN
        serialized = serialize_needed_grade(needed)

        # THEN
        self.assertEqual({"outcome": "achievable", "needed_grade": 83.3, "category": "Exams",
                          "assignment": {"title": "Final", "points_possible": 100.0, "needed_points": 83.3},
                          "grade_band": None}, serialized)

    def test_needed_grade_for_an_unreachable_gpa_names_the_top_band(self) -> None:
        # GIVEN
        needed = {"state": "gpa_unreachable", "needed_grade": 0, "weighted": True,
                  "category": {"id": 3, "title": "Exams"}, "assignment": None,
                  "grade_band": {"min_grade": 97.0, "grade_point": 4.0, "label": "A+"}}

        # WHEN
        serialized = serialize_needed_grade(needed)

        # THEN
        self.assertEqual({"outcome": "gpa_unreachable", "needed_grade": 0, "category": "Exams", "assignment": None,
                          "grade_band": {"label": "A+", "min_grade": 97.0, "grade_point": 4.0}},
                         serialized)


class TestNotes(unittest.TestCase):
    def test_delta_to_text(self) -> None:
        # WHEN / THEN
        self.assertEqual("Mitosis\nProphase first", delta_to_text(
            {"ops": [{"insert": "Mitosis\n", "attributes": {"header": 1}}, {"insert": {"image": "x.png"}},
                     {"insert": "Prophase first\n"}]}))
        self.assertEqual("plain", delta_to_text('{"ops": [{"insert": "plain"}]}'))
        self.assertEqual("", delta_to_text(None))

    def test_notes_keep_helium_order_and_trim_text(self) -> None:
        # GIVEN
        notes = [{"id": 1, "title": "Old", "content": {"ops": [{"insert": "a"}]},
                  "updated_at": "2026-09-01T00:00:00Z"},
                 {"id": 2, "title": "New", "content": {"ops": [{"insert": "b" * (MAX_NOTE_CHARS + 50)}]},
                  "updated_at": "2026-09-05T15:00:00Z", "linked_entity_title": "Lab 1",
                  "linked_entity_type": "homework", "course_id": 10}]

        # WHEN
        serialized = serialize_notes(notes, [{"id": 10, "title": "BIO 151"}], CHICAGO)

        # THEN
        self.assertEqual(["Old", "New"], [note["title"] for note in serialized])
        new = serialized[1]
        self.assertEqual(("Lab 1", "homework", "BIO 151"), (new["linked_to"], new["linked_type"], new["class"]))
        self.assertEqual("2026-09-05T10:00:00-05:00", new["updated"])
        self.assertEqual(MAX_NOTE_CHARS, len(new["text"]))

    def test_class_note_names_its_class(self) -> None:
        # GIVEN
        notes = [{"id": 3, "title": "", "content": {"ops": [{"insert": "Office hours Tuesday\n"}]},
                  "updated_at": "2026-09-05T15:00:00Z", "linked_entity_title": "BIO 151",
                  "linked_entity_type": "course", "course_id": 10}]

        # WHEN
        serialized = serialize_notes(notes, [{"id": 10, "title": "BIO 151"}], CHICAGO)

        # THEN
        self.assertEqual(("BIO 151", "course", "BIO 151", "Office hours Tuesday"),
                         (serialized[0]["linked_to"], serialized[0]["linked_type"], serialized[0]["class"],
                          serialized[0]["text"].strip()))
