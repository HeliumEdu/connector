import unittest
from datetime import date, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from helium_connector.models import Category, Course, CourseGroup, CycleSlot, DayCycleSchedule, Event, Homework, \
    Meeting, WeeklySchedule, WeekRotationSchedule
from helium_connector.planning import (
    MAX_ASSIGNMENTS,
    MAX_EVENTS,
    build_import,
    describe_rule,
    existing_term,
    explain_platform_errors,
    occurrences,
    summarize,
    to_confirm,
    validate,
    warnings,
)
from helium_connector.platform_enums import RECURRENCE_HORIZON_YEARS
from tests.testcase import given_a_plan


class TestBuildImport(unittest.TestCase):
    def test_term_and_classes(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        data = build_import(plan, ZoneInfo("America/Chicago")).data

        # THEN
        self.assertEqual([{"id": 1, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18",
                           "shown_on_calendar": True, "exceptions": "20260907,20261126"}], data["course_groups"])
        bio, eng = data["courses"]
        self.assertEqual({"id": 1, "course_group": 1, "title": "BIO 151", "credits": "4.00", "is_online": False,
                          "start_date": "2026-08-31", "end_date": "2026-12-18", "exceptions": "",
                          "room": "Bagley 131", "teacher_name": "Dr. Smith", "teacher_email": "smith@example.edu"},
                         bio)
        self.assertEqual("0.00", eng["credits"])
        self.assertNotIn("room", eng)
        self.assertNotIn("website", eng)

    def test_meetings_on_the_same_day_get_separate_schedules(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        schedules = build_import(plan, ZoneInfo("America/Chicago")).data["course_schedules"]

        # THEN
        self.assertEqual(2, len(schedules))
        lecture, lab = schedules
        self.assertEqual("0101010", lecture["days_of_week"])
        self.assertEqual(("10:00:00", "10:50:00"), (lecture["mon_start_time"], lecture["mon_end_time"]))
        self.assertEqual(("00:00:00", "00:00:00"), (lecture["tue_start_time"], lecture["tue_end_time"]))
        self.assertEqual("0100000", lab["days_of_week"])
        self.assertEqual(("14:00:00", "16:50:00"), (lab["mon_start_time"], lab["mon_end_time"]))
        self.assertEqual(("00:00:00", "00:00:00"), (lab["wed_start_time"], lab["wed_end_time"]))

    def test_categories_resolve_case_insensitively(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        data = build_import(plan, ZoneInfo("America/Chicago")).data

        # THEN
        categories = {category["title"]: category["id"] for category in data["categories"]}
        problem_set, final, essay = data["homework"]
        self.assertEqual(categories["Homework"], problem_set["category"])
        self.assertEqual(categories["Exams"], final["category"])
        self.assertIsNone(essay["category"])
        self.assertEqual("40.00", data["categories"][0]["weight"])

    def test_due_times_carry_the_offset_for_their_date(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        problem_set, final, _ = build_import(plan, ZoneInfo("America/Chicago")).data["homework"]

        # THEN
        self.assertEqual("2026-09-14T23:59:00-05:00", problem_set["start"])
        self.assertEqual(problem_set["start"], problem_set["end"])
        self.assertFalse(problem_set["show_end_time"])
        self.assertEqual("2026-12-14T08:00:00-06:00", final["start"])
        self.assertEqual("2026-12-14T10:00:00-06:00", final["end"])
        self.assertTrue(final["show_end_time"])
        self.assertEqual("-1/100", final["current_grade"])
        self.assertNotIn("priority", final, "Helium's default applies")
        self.assertNotIn("completed", final, "Helium's default applies")

    def test_due_times_in_a_positive_offset_zone(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        problem_set = build_import(plan, ZoneInfo("Europe/Amsterdam")).data["homework"][0]

        # THEN
        self.assertEqual("2026-09-14T23:59:00+02:00", problem_set["start"])

    def test_all_day_items_end_at_the_next_local_midnight(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="ENG 101", assignments=[
            Homework(title="Essay", due_date=date(2026, 10, 31)),
            Homework(title="Reading", due_date=date(2026, 11, 1))])])

        # WHEN
        essay, reading = build_import(plan, ZoneInfo("America/Chicago")).data["homework"]

        # THEN
        self.assertTrue(essay["all_day"])
        self.assertEqual("2026-10-31T00:00:00-05:00", essay["start"])
        self.assertEqual("2026-11-01T00:00:00-05:00", essay["end"])
        self.assertEqual("2026-11-01T00:00:00-05:00", reading["start"])
        self.assertEqual("2026-11-02T00:00:00-06:00", reading["end"])

    def test_repeating_event_is_one_row_with_its_rule_and_skipped_no_class_dates(self) -> None:
        # GIVEN
        plan = given_a_plan(events=[Event(title="Office Hours", start_date=date(2026, 8, 31), start_time=time(15),
                                          end_time=time(16),
                                          recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU;UNTIL=20260914")])

        # WHEN
        payload = build_import(plan, ZoneInfo("America/Chicago"))

        # THEN
        self.assertEqual([{"id": 1, "title": "Office Hours", "all_day": False, "show_end_time": True,
                           "start": "2026-08-31T15:00:00-05:00", "end": "2026-08-31T16:00:00-05:00",
                           "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU;UNTIL=20260915T045959Z",
                           "exception_dates": ["2026-09-07T20:00:00Z"]}], payload.data["events"])
        self.assertEqual("Event 'Office Hours' repeating from 2026-08-31", payload.labels[("events", 1)])

    def test_local_until_becomes_the_end_of_that_local_day_in_utc(self) -> None:
        for zone, until in (("Europe/Amsterdam", "20261211T225959Z"), ("America/Los_Angeles", "20261212T075959Z")):
            with self.subTest(zone=zone):
                # GIVEN
                plan = given_a_plan(courses=[Course(title="MAT 25", assignments=[
                    Homework(title="Problem Set", due_date=date(2026, 9, 4), due_time=time(23, 59),
                             recurrence_rule="FREQ=WEEKLY;BYDAY=FR;UNTIL=20261211")])])

                # WHEN
                payload = build_import(plan, ZoneInfo(zone))

                # THEN
                homework = payload.data["homework"][0]
                self.assertEqual(1, len(payload.data["homework"]))
                self.assertEqual(f"FREQ=WEEKLY;BYDAY=FR;UNTIL={until}", homework["recurrence_rule"])
                self.assertEqual("-1/100", homework["current_grade"])
                self.assertNotIn("exception_dates", homework)
                self.assertEqual("Assignment 'Problem Set' (MAT 25) repeating from 2026-09-04",
                                 payload.labels[("homework", 1)])

    def test_rules_without_until_end_at_the_end_of_the_last_local_day(self) -> None:
        for zone, until in (("Europe/Amsterdam", "20261218T225959Z"), ("America/Chicago", "20261219T055959Z")):
            with self.subTest(zone=zone):
                # GIVEN
                plan = given_a_plan(courses=[Course(title="MAT 25", assignments=[
                    Homework(title="Lab", due_date=date(2026, 9, 2),
                             recurrence_rule="freq=weekly;interval=2;byday=we"),
                    Homework(title="Reflection", due_date=date(2026, 9, 30),
                             recurrence_rule="RRULE:FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3")])],
                    events=[Event(title="Office Hours", start_date=date(2026, 9, 1), start_time=time(15),
                                  recurrence_rule="FREQ=WEEKLY;BYDAY=TU")])

                # WHEN
                data = build_import(plan, ZoneInfo(zone)).data

                # THEN
                lab, reflection = data["homework"]
                self.assertEqual(f"FREQ=WEEKLY;INTERVAL=2;BYDAY=WE;UNTIL={until}", lab["recurrence_rule"])
                self.assertEqual("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3", reflection["recurrence_rule"])
                self.assertEqual(f"FREQ=WEEKLY;BYDAY=TU;UNTIL={until}", data["events"][0]["recurrence_rule"])

    def test_all_day_repeat_skips_no_class_dates_at_local_midnight(self) -> None:
        for zone, skipped in (("Europe/Amsterdam", "2026-09-06T22:00:00Z"),
                              ("America/Los_Angeles", "2026-09-07T07:00:00Z")):
            with self.subTest(zone=zone):
                # GIVEN
                plan = given_a_plan(events=[Event(title="Gym", start_date=date(2026, 8, 31),
                                                  recurrence_rule="FREQ=WEEKLY;BYDAY=MO;COUNT=3")])

                # WHEN
                event = build_import(plan, ZoneInfo(zone)).data["events"][0]

                # THEN
                self.assertTrue(event["all_day"])
                self.assertEqual([skipped], event["exception_dates"])

    def test_repeating_assignment_skips_term_and_class_no_class_dates(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="MAT 25", no_class_dates=[date(2026, 9, 21)], assignments=[
            Homework(title="Quiz", due_date=date(2026, 8, 31), due_time=time(9),
                     recurrence_rule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20260928")])])

        # WHEN
        homework = build_import(plan, ZoneInfo("America/Chicago")).data["homework"][0]

        # THEN
        self.assertEqual("2026-08-31T09:00:00-05:00", homework["start"])
        self.assertEqual(["2026-09-07T14:00:00Z", "2026-09-21T14:00:00Z"], homework["exception_dates"],
                         "Sep 7 is the term's no-class date, Sep 21 the class's")

    def test_weekly_exceptions_keep_the_local_time_across_dst(self) -> None:
        # GIVEN
        plan = given_a_plan(course_group=CourseGroup(title="Fall 2026", start_date=date(2026, 8, 31),
                                                     end_date=date(2026, 12, 18), no_class_dates=[date(2026, 11, 2)]),
                            events=[Event(title="Seminar", start_date=date(2026, 10, 26), start_time=time(9),
                                          recurrence_rule="FREQ=WEEKLY;BYDAY=MO;COUNT=2")])

        # WHEN
        event = build_import(plan, ZoneInfo("America/Chicago")).data["events"][0]

        # THEN
        self.assertEqual("2026-10-26T09:00:00-05:00", event["start"])
        self.assertEqual(["2026-11-02T15:00:00Z"], event["exception_dates"], "09:00 CST, after DST ends on Nov 1")

    def test_ids_are_unique_per_section_and_resolve(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        data = build_import(plan, ZoneInfo("America/Chicago")).data

        # THEN
        for section in ("courses", "course_schedules", "categories", "homework", "events"):
            ids = [row["id"] for row in data[section]]
            self.assertEqual(len(ids), len(set(ids)), section)
        course_ids = {course["id"] for course in data["courses"]}
        self.assertTrue(all(row["course"] in course_ids for row in data["course_schedules"] + data["homework"]))


class TestScheduleRows(unittest.TestCase):
    def given_rows(self, *schedules: WeeklySchedule | WeekRotationSchedule | DayCycleSchedule) -> list[dict]:
        plan = given_a_plan(courses=[Course(title="BIO 151", schedules=list(schedules))])
        return build_import(plan, ZoneInfo("America/Chicago")).data["course_schedules"]

    def test_week_rotation(self):
        # GIVEN
        schedule = WeekRotationSchedule(pattern="week_rotation", week="B", week_a_starts_on=date(2026, 9, 2),
                                        meetings=[Meeting(days=["tuesday", "thursday"], start_time=time(13),
                                                          end_time=time(14, 15))])

        # WHEN
        rows = self.given_rows(schedule)

        # THEN
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(("0010100", True, 1, "2026-09-02"),
                         (row["days_of_week"], row["is_week_based"], row["week_offset"], row["anchor_date"]))
        self.assertEqual(("13:00:00", "14:15:00"), (row["tue_start_time"], row["tue_end_time"]))
        self.assertEqual(("00:00:00", "00:00:00"), (row["mon_start_time"], row["mon_end_time"]))
        self.assertNotIn("cycle_length", row)

    def test_week_a(self):
        # GIVEN
        schedule = WeekRotationSchedule(pattern="week_rotation", week="A", week_a_starts_on=date(2026, 9, 2),
                                        meetings=[Meeting(days=["friday"], start_time=time(9), end_time=time(10))])

        # WHEN
        rows = self.given_rows(schedule)

        # THEN
        self.assertEqual(0, rows[0]["week_offset"])

    def test_day_cycle_groups_days_that_share_a_time(self):
        # GIVEN
        schedule = DayCycleSchedule(pattern="day_cycle", cycle_length=6, day_one=date(2026, 8, 31), slots=[
            CycleSlot(cycle_days=[1, 3], start_time=time(9), end_time=time(9, 50)),
            CycleSlot(cycle_days=[5], start_time=time(13), end_time=time(14, 30))])

        # WHEN
        rows = self.given_rows(schedule)

        # THEN
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual((6, "2026-08-31"), (row["cycle_length"], row["anchor_date"]))
        self.assertEqual([{"indices": [1, 3], "start_time": "09:00:00", "end_time": "09:50:00"},
                          {"indices": [5], "start_time": "13:00:00", "end_time": "14:30:00"}], row["cycle_slots"])
        self.assertEqual("0000000", row["days_of_week"])
        self.assertTrue(all(row[f"{day}_start_time"] == "00:00:00" for day in ("sun", "mon", "sat")))
        self.assertNotIn("is_week_based", row)

    def test_day_cycle_meeting_twice_on_a_cycle_day_gets_a_second_row(self):
        # GIVEN
        schedule = DayCycleSchedule(pattern="day_cycle", cycle_length=2, day_one=date(2026, 8, 31), slots=[
            CycleSlot(cycle_days=[1, 2], start_time=time(8), end_time=time(9)),
            CycleSlot(cycle_days=[1], start_time=time(13), end_time=time(14))])

        # WHEN
        rows = self.given_rows(schedule)

        # THEN
        self.assertEqual([[{"indices": [1, 2], "start_time": "08:00:00", "end_time": "09:00:00"}],
                          [{"indices": [1], "start_time": "13:00:00", "end_time": "14:00:00"}]],
                         [row["cycle_slots"] for row in rows])

    def test_part_of_term_window(self):
        # GIVEN
        schedule = WeeklySchedule(pattern="weekly", start_date=date(2026, 9, 14), end_date=date(2026, 10, 23),
                                  meetings=[Meeting(days=["monday", "monday"], start_time=time(9),
                                                    end_time=time(10))])

        # WHEN
        rows = self.given_rows(schedule)

        # THEN
        self.assertEqual(1, len(rows))
        self.assertEqual(("2026-09-14", "2026-10-23", "0100000"),
                         (rows[0]["start_date"], rows[0]["end_date"], rows[0]["days_of_week"]))

    def test_window_is_omitted_when_the_class_dates_apply(self):
        # GIVEN
        schedule = WeeklySchedule(pattern="weekly",
                                  meetings=[Meeting(days=["monday"], start_time=time(9), end_time=time(10))])

        # WHEN
        rows = self.given_rows(schedule)

        # THEN
        self.assertNotIn("start_date", rows[0])
        self.assertNotIn("end_date", rows[0])


class TestValidateSchedules(unittest.TestCase):
    def test_day_cycle_problems(self):
        # GIVEN
        plan = given_a_plan(courses=[Course(title="ART", no_class_dates=[date(2026, 9, 1)], schedules=[
            DayCycleSchedule(pattern="day_cycle", cycle_length=6, day_one=date(2026, 9, 5), slots=[
                CycleSlot(cycle_days=[0, 7, 3, 3], start_time=time(10), end_time=time(9))]),
            DayCycleSchedule(pattern="day_cycle", cycle_length=2, day_one=date(2026, 9, 1), slots=[
                CycleSlot(cycle_days=[1], start_time=time(9), end_time=time(10))]),
            DayCycleSchedule(pattern="day_cycle", cycle_length=2, day_one=date(2026, 9, 7), slots=[
                CycleSlot(cycle_days=[2], start_time=time(9), end_time=time(10))])])])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([
            "Class 'ART', 6-day cycle schedule: cycle Day 1 (2026-09-05) is a Saturday; the cycle only counts "
            "school days, Monday to Friday.",
            "Class 'ART', 6-day cycle schedule: a time slot lists the same cycle day more than once.",
            "Class 'ART', A/B Day schedule: cycle Day 1 (2026-09-01) is a no-class date.",
            "Class 'ART', A/B Day schedule: cycle Day 1 (2026-09-07) is a no-class date.",
        ], problems)

    def test_day_one_outside_the_class(self):
        # GIVEN
        plan = given_a_plan(courses=[Course(title="SPAN 3", schedules=[
            DayCycleSchedule(pattern="day_cycle", cycle_length=2, day_one=date(2025, 1, 6), slots=[
                CycleSlot(cycle_days=[1], start_time=time(9), end_time=time(10))]),
            DayCycleSchedule(pattern="day_cycle", cycle_length=2, day_one=date(2026, 9, 2),
                             start_date=date(2026, 9, 14), slots=[
                                 CycleSlot(cycle_days=[2], start_time=time(9), end_time=time(10))])])])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual(["Class 'SPAN 3', A/B Day schedule: cycle Day 1 (2025-01-06) is outside the class's dates "
                          "(2026-08-31 to 2026-12-18). Use a Day 1 within the class's dates."], problems)

    def test_schedule_window_problems(self):
        # GIVEN
        plan = given_a_plan(courses=[Course(title="LAB", end_date=date(2026, 10, 30), schedules=[
            WeeklySchedule(pattern="weekly", start_date=date(2026, 10, 1), end_date=date(2026, 9, 1),
                           meetings=[Meeting(days=["monday"], start_time=time(9), end_time=time(10))]),
            WeekRotationSchedule(pattern="week_rotation", week="A", week_a_starts_on=date(2026, 8, 31),
                                 end_date=date(2026, 11, 20),
                                 meetings=[Meeting(days=["friday"], start_time=time(9), end_time=time(9))])])])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([
            "Class 'LAB', Week A schedule: its dates (2026-08-31 to 2026-11-20) fall outside the class's dates "
            "(2026-08-31 to 2026-10-30).",
        ], problems)

    def test_unknown_pattern_is_rejected(self):
        # WHEN / THEN
        with self.assertRaises(ValueError):
            Course.model_validate({"title": "X", "schedules": [{"pattern": "block", "meetings": []}]})


class TestDescribeSchedules(unittest.TestCase):
    def test_summary_describes_every_pattern(self):
        # GIVEN
        plan = given_a_plan(courses=[Course(title="BIO 151", schedules=[
            WeekRotationSchedule(pattern="week_rotation", week="B", week_a_starts_on=date(2026, 9, 2),
                                 meetings=[Meeting(days=["tuesday"], start_time=time(13), end_time=time(14))]),
            DayCycleSchedule(pattern="day_cycle", cycle_length=2, day_one=date(2026, 8, 31),
                             start_date=date(2026, 9, 8),
                             slots=[CycleSlot(cycle_days=[2], start_time=time(9), end_time=time(10))]),
            DayCycleSchedule(pattern="day_cycle", cycle_length=6, day_one=date(2026, 8, 31),
                             end_date=date(2026, 10, 9),
                             slots=[CycleSlot(cycle_days=[1, 4], start_time=time(9), end_time=time(10))])])])

        # WHEN
        meets = summarize(plan)["classes"][0]["meets"]

        # THEN
        self.assertEqual(["Week B (Week A includes 2026-09-02): Tue 13:00-14:00",
                          "A/B Day (A Day on 2026-08-31): B Day 09:00-10:00, from 2026-09-08",
                          "6-day cycle (Day 1 on 2026-08-31): Days 1 and 4 09:00-10:00, until 2026-10-09"], meets)


class TestValidate(unittest.TestCase):
    def test_valid_plan(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([], problems)

    def test_class_problems(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(
            title="CHEM 101",
            teacher_email="not an email",
            end_date=date(2027, 1, 15),
            schedules=[WeeklySchedule(pattern="weekly", meetings=[
                Meeting(days=["tuesday"], start_time=time(11), end_time=time(10)),
                Meeting(days=["thursday"], start_time=time(0), end_time=time(1))])],
            categories=[Category(title="Labs", weight=Decimal(70)), Category(title="labs", weight=Decimal(40))],
            homework=[Homework(title="Lab 1", category="Quizzes", due_date=date(2026, 9, 3)),
                      Homework(title="Lab 2", due_date=date(2026, 9, 10), end_time=time(12)),
                      Homework(title="Lab 3", due_date=date(2026, 9, 17), due_time=time(12), end_time=time(11))])])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([
            "Class 'CHEM 101': its dates (2026-08-31 to 2027-01-15) fall outside the term (2026-08-31 to "
            "2026-12-18).",
            "Class 'CHEM 101': category 'labs' is listed more than once.",
            "Class 'CHEM 101', assignment 'Lab 1': category 'Quizzes' isn't one of the class's categories.",
            "Class 'CHEM 101', assignment 'Lab 2': an end time needs a due time.",
        ], problems)

    def test_due_date_outside_the_class(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="ART 110", end_date=date(2026, 10, 16),
                                            homework=[Homework(title="Portfolio", due_date=date(2026, 12, 11))])])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual(["Class 'ART 110', assignment 'Portfolio': due 2026-12-11, outside the class's dates "
                          "(2026-08-31 to 2026-10-16)."], problems)

    def test_event_problems(self) -> None:
        # GIVEN
        plan = given_a_plan(events=[Event(title="Review", start_date=date(2026, 12, 10), end_time=time(17))])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual(["Event 'Review': an end time needs a start time."], problems)

    def test_repeat_problems(self) -> None:
        # GIVEN
        plan = given_a_plan(
            courses=[Course(title="MAT 25", assignments=[
                Homework(title="Unreadable", due_date=date(2026, 9, 4), recurrence_rule="FREQ=WEEKLY;BYDAY=XX"),
                Homework(title="Timed until", due_date=date(2026, 9, 4),
                         recurrence_rule="FREQ=WEEKLY;UNTIL=20261211T120000Z"),
                Homework(title="Never", due_date=date(2026, 9, 4),
                         recurrence_rule="FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30"),
                Homework(title="Off its days", due_date=date(2026, 9, 3), recurrence_rule="FREQ=WEEKLY;BYDAY=FR")])],
            events=[Event(title="Holiday only", start_date=date(2026, 9, 7), recurrence_rule="FREQ=DAILY;COUNT=1")])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([
            "Class 'MAT 25', assignment 'Unreadable': its recurrence_rule `FREQ=WEEKLY;BYDAY=XX` isn't an iCal "
            "RRULE.",
            "Class 'MAT 25', assignment 'Timed until': its recurrence_rule `FREQ=WEEKLY;UNTIL=20261211T120000Z` has "
            "an `UNTIL` that isn't a local date, like 20261211.",
            "Class 'MAT 25', assignment 'Never': its recurrence_rule `FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30` has no "
            "dates within 5 years.",
            "Class 'MAT 25', assignment 'Off its days': 2026-09-03 isn't an occurrence of its recurrence_rule "
            "`FREQ=WEEKLY;BYDAY=FR`; the item's date is its first occurrence.",
            "Event 'Holiday only': every occurrence of its recurrence_rule `FREQ=DAILY;COUNT=1` falls on a no-class "
            "date."], problems)

    def test_repeating_assignment_past_its_class_is_a_problem(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="MAT 25", end_date=date(2026, 12, 4), assignments=[
            Homework(title="Until", due_date=date(2026, 9, 4), recurrence_rule="FREQ=WEEKLY;BYDAY=FR;UNTIL=20261211"),
            Homework(title="Count", due_date=date(2026, 9, 4), recurrence_rule="FREQ=WEEKLY;BYDAY=FR;COUNT=20"),
            Homework(title="Within", due_date=date(2026, 9, 4), recurrence_rule="FREQ=WEEKLY;BYDAY=FR;UNTIL=20261204"),
            Homework(title="Open", due_date=date(2026, 9, 4), recurrence_rule="FREQ=WEEKLY;BYDAY=FR")])])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([
            "Class 'MAT 25', assignment 'Until': its recurrence_rule `FREQ=WEEKLY;BYDAY=FR;UNTIL=20261211` has "
            "occurrences through 2026-12-11, outside the class's dates (2026-08-31 to 2026-12-04).",
            "Class 'MAT 25', assignment 'Count': its recurrence_rule `FREQ=WEEKLY;BYDAY=FR;COUNT=20` has "
            "occurrences through 2027-01-15, outside the class's dates (2026-08-31 to 2026-12-04)."], problems)

    def test_caps(self) -> None:
        # GIVEN
        plan = given_a_plan(
            events=[Event(title=f"Study block {index}", start_date=date(2026, 8, 31), recurrence_rule="FREQ=DAILY")
                    for index in range(5)])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([f"The plan has more than {MAX_EVENTS} events, counting each repeating occurrence; "
                          f"the connector imports at most {MAX_EVENTS} at once."], problems)

    def test_assignment_cap_counts_each_occurrence(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title=f"Class {index}", assignments=[
            Homework(title="Daily log", due_date=date(2026, 8, 31),
                     recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")])
            for index in range(13)])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([f"The plan has more than {MAX_ASSIGNMENTS} assignments, counting each repeating occurrence; "
                          f"the connector imports at most {MAX_ASSIGNMENTS} at once."], problems)

    def test_repeat_expansion_stops_at_a_count_whatever_the_frequency(self) -> None:
        # GIVEN
        event = Event(title="Ping", start_date=date(2026, 8, 31), start_time=time(9), recurrence_rule="FREQ=SECONDLY")

        # WHEN
        days = occurrences(event, [], date(2026, 12, 18))

        # THEN
        self.assertEqual(max(MAX_ASSIGNMENTS, MAX_EVENTS) + 1, len(days))

    def test_repeat_expansion_stops_at_the_horizon(self) -> None:
        # GIVEN
        event = Event(title="Journal", start_date=date(2026, 8, 31), recurrence_rule="FREQ=WEEKLY;COUNT=1000000")
        horizon = date(2026 + RECURRENCE_HORIZON_YEARS, 8, 31)

        # WHEN
        days = occurrences(event, [], date(2026, 12, 18))

        # THEN
        self.assertEqual(date(2026, 8, 31), days[0])
        self.assertLessEqual(days[-1], horizon)
        self.assertGreater(days[-1] + timedelta(weeks=1), horizon)

    def test_oversized_lists_are_rejected_by_the_schema(self) -> None:
        # WHEN / THEN
        with self.assertRaises(ValueError):
            CourseGroup(title="Fall", start_date=date(2026, 8, 31), end_date=date(2026, 12, 18),
                        no_class_dates=[date(2026, 9, 1)] * 367)


class TestWarningsAndSummary(unittest.TestCase):
    def test_warnings(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="HIST 200", categories=[Category(title="Papers",
                                                                                   weight=Decimal(85))],
                                            homework=[Homework(title="Quiz 1", due_date=date(2026, 9, 9))])])

        # WHEN
        notes = warnings(plan)

        # THEN
        self.assertEqual(["Class 'HIST 200': category weights add up to 85%, not 100%.",
                          "Class 'HIST 200': 1 assignment(s) have no category and go in Uncategorized."], notes)

    def test_to_confirm(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        gaps = to_confirm(plan)

        # THEN
        self.assertEqual(["Class 'ENG 101': credits aren't set, so it imports with 0 credits and isn't counted in "
                          "GPA.",
                          "Class 'ENG 101': no grading categories, so its assignments go in Uncategorized.",
                          "Class 'ENG 101': 1 assignment(s) have no due time, so they're all-day."], gaps)

    def test_to_confirm_in_person_class_without_details(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[
            Course(title="CHEM 142", credits=Decimal(5)),
            Course(title="MATH 124", credits=Decimal(5),
                   schedules=[WeeklySchedule(pattern="weekly", meetings=[
                       Meeting(days=["tuesday"], start_time=time(9), end_time=time(10))])])])

        # WHEN
        gaps = to_confirm(plan)

        # THEN
        self.assertEqual(["Class 'CHEM 142': no meeting times, so it won't show on the calendar.",
                          "Class 'CHEM 142': no grading categories, so its assignments go in Uncategorized.",
                          "Class 'CHEM 142': no assignments.",
                          "Class 'MATH 124': no room, and it isn't marked online.",
                          "Class 'MATH 124': no grading categories, so its assignments go in Uncategorized.",
                          "Class 'MATH 124': no assignments."], gaps)

    def test_existing_term(self) -> None:
        # WHEN
        term = existing_term({"id": 4, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18",
                              "exceptions": "20260907,20261126"})

        # THEN
        self.assertEqual("Fall 2026", term.title)
        self.assertEqual((date(2026, 8, 31), date(2026, 12, 18)), (term.start_date, term.end_date))
        self.assertEqual([date(2026, 9, 7), date(2026, 11, 26)], term.no_class_dates)

    def test_existing_term_without_exceptions(self) -> None:
        # WHEN
        term = existing_term({"id": 4, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18",
                              "exceptions": ""})

        # THEN
        self.assertEqual([], term.no_class_dates)

    def test_repeated_classes_and_events_outside_the_term(self) -> None:
        # GIVEN
        plan = given_a_plan(
            courses=[Course(title="BIO 151", credits=Decimal(4), is_online=True),
                     Course(title="bio 151", credits=Decimal(1), is_online=True)],
            events=[Event(title="Orientation", start_date=date(2026, 8, 20)),
                    Event(title="Review", start_date=date(2026, 12, 7),
                          recurrence_rule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20270111")])

        # WHEN
        notes = warnings(plan)

        # THEN
        self.assertEqual(["Class 'BIO 151' is listed 2 times; each becomes its own class in Helium.",
                          "Event 'Orientation' (on 2026-08-20) falls outside the term (2026-08-31 to 2026-12-18).",
                          "Event 'Review' (2026-12-07 to 2027-01-11) falls outside the term (2026-08-31 to "
                          "2026-12-18)."], notes)

    def test_warnings_alongside_problems(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="HIST 200", is_online=True,
                                            categories=[Category(title="Papers", weight=Decimal(90)),
                                                        Category(title="Papers", weight=Decimal(0))])])

        # WHEN
        problems, notes = validate(plan), warnings(plan)

        # THEN
        self.assertEqual(["Class 'HIST 200': category 'Papers' is listed more than once."], problems)
        self.assertIn("Class 'HIST 200': category weights add up to 90%, not 100%.", notes)

    def test_summary(self) -> None:
        # GIVEN
        plan = given_a_plan()

        # WHEN
        summary = summarize(plan)

        # THEN
        self.assertEqual({"classes": 2, "categories": 2, "assignments": 3, "events": 2}, summary["totals"])
        bio = summary["classes"][0]
        self.assertEqual([], bio["repeating"])
        self.assertEqual(["Mon/Wed/Fri 10:00-10:50; Mon 14:00-16:50"], bio["meets"])
        self.assertEqual("weighted", bio["grading"])
        self.assertEqual(["Homework (40%)", "Exams (60%)"], bio["categories"])
        self.assertEqual(("2026-09-14", "2026-12-14"), (bio["first_due"], bio["last_due"]))
        self.assertIsNone(summary["classes"][1]["grading"])
        self.assertEqual([{"title": "Office Hours", "time": "15:00-16:00", "first": "2026-09-01",
                           "last": "2026-09-08", "occurrences": 2, "repeats": "weekly on Tue until 2026-09-08"}],
                         summary["events"])

    def test_summary_counts_each_occurrence_of_a_repeating_assignment(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="MAT 25", assignments=[
            Homework(title="Problem Set", due_date=date(2026, 8, 31),
                     recurrence_rule="FREQ=WEEKLY;BYDAY=MO,FR;UNTIL=20260918"),
            Homework(title="Final", due_date=date(2026, 12, 14))])])

        # WHEN
        summary = summarize(plan)

        # THEN
        mat = summary["classes"][0]
        self.assertEqual(6, mat["assignments"], "Aug 31, Sep 4, 11, 14, 18 (Sep 7 skipped), plus the final")
        self.assertEqual([{"title": "Problem Set", "repeats": "weekly on Mon, Fri until 2026-09-18",
                           "assignments": 5}], mat["repeating"])
        self.assertEqual(("2026-08-31", "2026-12-14"), (mat["first_due"], mat["last_due"]))
        self.assertEqual(6, summary["totals"]["assignments"])

    def test_open_ended_event_is_counted_through_the_term(self) -> None:
        # GIVEN
        plan = given_a_plan(events=[Event(title="Club", start_date=date(2026, 12, 4), start_time=time(18),
                                          recurrence_rule="FREQ=WEEKLY;BYDAY=FR")])

        # WHEN
        summary = summarize(plan)

        # THEN
        self.assertEqual([{"title": "Club", "time": "18:00", "first": "2026-12-04", "last": "2026-12-18",
                           "occurrences": 3, "repeats": "weekly on Fri"}], summary["events"])

    def test_describe_rule(self) -> None:
        for rule, description in (("FREQ=DAILY", "daily"),
                                  ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=20261211",
                                   "every 2 weeks on Mon, Wed until 2026-12-11"),
                                  ("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=4", "monthly on the last day, 4 times"),
                                  ("FREQ=MONTHLY;BYMONTHDAY=15", "monthly on day 15"),
                                  ("FREQ=YEARLY", "yearly"),
                                  ("FREQ=HOURLY", "FREQ=HOURLY")):
            with self.subTest(rule=rule):
                # THEN
                self.assertEqual(description, describe_rule(rule))

    def test_summary_of_points_based_grading_and_all_day_events(self) -> None:
        # GIVEN
        plan = given_a_plan(courses=[Course(title="MAT 25", categories=[Category(title="Tests"),
                                                                        Category(title="Homework")])],
                            events=[Event(title="Add/drop deadline", start_date=date(2026, 9, 11))])

        # WHEN
        summary = summarize(plan)

        # THEN
        self.assertEqual("points-based", summary["classes"][0]["grading"])
        self.assertEqual(["Tests", "Homework"], summary["classes"][0]["categories"])
        self.assertEqual("all day", summary["events"][0]["time"])


class TestExplainPlatformErrors(unittest.TestCase):
    def test_row_errors_name_the_item(self) -> None:
        # GIVEN
        payload = build_import(given_a_plan(), ZoneInfo("America/Chicago"))
        errors = {"homework": ["homework id 2, current_grade: Ensure this value is valid."],
                  "courses": "Unresolved `course_group` reference `7` in courses.",
                  "details": "Invalid JSON in file."}

        # WHEN
        messages = explain_platform_errors(errors, payload.labels)

        # THEN
        self.assertEqual(["Assignment 'Final Exam' (BIO 151): current grade: Ensure this value is valid.",
                          "courses: Unresolved `course_group` reference `7` in courses.",
                          "details: Invalid JSON in file."], messages)

    def test_unexplained(self) -> None:
        # WHEN
        messages = explain_platform_errors(None, {})

        # THEN
        self.assertEqual(["Helium rejected the import without saying why."], messages)
