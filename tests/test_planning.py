import unittest
from datetime import date, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from helium_connector.models import Category, Course, CourseGroup, CycleSlot, DayCycleSchedule, Event, Homework, \
    Meeting, Repeat, WeeklySchedule, WeekRotationSchedule
from helium_connector.planning import (
    MAX_COURSES,
    MAX_EVENTS,
    MAX_REPEAT_DAYS,
    build_import,
    explain_platform_errors,
    summarize,
    validate,
    warnings,
)
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
        self.assertEqual(("-1/100", False, 50), (final["current_grade"], final["completed"], final["priority"]))

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

    def test_repeating_events_expand_and_skip_no_class_dates(self) -> None:
        # GIVEN
        plan = given_a_plan(events=[Event(title="Office Hours", start_date=date(2026, 9, 1), start_time=time(15),
                                          end_time=time(16),
                                          repeat=Repeat(days=["monday", "tuesday"], until=date(2026, 9, 14)))])

        # WHEN
        payload = build_import(plan, ZoneInfo("America/Chicago"))

        # THEN
        starts = [event["start"] for event in payload.data["events"]]
        self.assertEqual(["2026-09-01T15:00:00-05:00", "2026-09-08T15:00:00-05:00", "2026-09-14T15:00:00-05:00"],
                         starts)
        self.assertEqual("Event 'Office Hours' on 2026-09-08", payload.labels[("events", 2)])
        self.assertTrue(all("recurrence_rule" not in event for event in payload.data["events"]))

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
            "Class 'ART', 6-day cycle schedule: cycle day(s) 0, 7 aren't in a 6-day cycle.",
            "Class 'ART', 6-day cycle schedule: a time slot lists the same cycle day more than once.",
            "Class 'ART', 6-day cycle schedule: the slot on Days 0, 7, 3, and 3 starts at 10:00, which isn't "
            "before its end (09:00).",
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
            "Class 'LAB', weekly schedule: its start date (2026-10-01) is after its end date (2026-09-01).",
            "Class 'LAB', Week A schedule: its dates (2026-08-31 to 2026-11-20) fall outside the class's dates "
            "(2026-08-31 to 2026-10-30).",
            "Class 'LAB', Week A schedule: the meeting on Fri starts at 09:00, which isn't before its end (09:00).",
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
            "Class 'CHEM 101': teacher email 'not an email' isn't an email address.",
            "Class 'CHEM 101', weekly schedule: the meeting on Tue starts at 11:00, which isn't before its end "
            "(10:00).",
            "Class 'CHEM 101', weekly schedule: meeting times on Thu can't start or end at midnight.",
            "Class 'CHEM 101': category 'labs' is listed more than once.",
            "Class 'CHEM 101': category weights add up to 110%, more than 100%.",
            "Class 'CHEM 101', assignment 'Lab 1': category 'Quizzes' isn't one of the class's categories.",
            "Class 'CHEM 101', assignment 'Lab 2': an end time needs a due time.",
            "Class 'CHEM 101', assignment 'Lab 3': its end time (11:00) isn't after its due time (12:00).",
        ], problems)

    def test_term_problems(self) -> None:
        # GIVEN
        plan = given_a_plan()
        plan.course_group.no_class_dates.append(date(2027, 1, 1))

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual(["Term 'Fall 2026': no-class date 2027-01-01 is outside 2026-08-31 to 2026-12-18."],
                         problems)

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
        plan = given_a_plan(events=[Event(title="Review", start_date=date(2026, 12, 10), end_time=time(17),
                                          repeat=Repeat(days=["thursday"], until=date(2026, 12, 1)))])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual(["Event 'Review': an end time needs a start time.",
                          "Event 'Review': it repeats until 2026-12-01, before its first date (2026-12-10)."],
                         problems)

    def test_caps(self) -> None:
        # GIVEN
        every_day = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
        plan = given_a_plan(
            courses=[Course(title=f"Class {index}") for index in range(MAX_COURSES + 1)],
            events=[Event(title=f"Study block {index}", start_date=date(2026, 8, 31),
                          repeat=Repeat(days=every_day, until=date(2026, 12, 18))) for index in range(5)])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertIn(f"The plan has {MAX_COURSES + 1} classes; one import takes at most {MAX_COURSES}.", problems)
        self.assertIn(f"The plan's events come to more than {MAX_EVENTS} after repeats; one import takes at most "
                      f"{MAX_EVENTS}.", problems)

    def test_unbounded_repeat_is_refused_before_expanding(self) -> None:
        # GIVEN
        plan = given_a_plan(events=[Event(title="Forever", start_date=date(2026, 8, 31),
                                          repeat=Repeat(days=["monday"], until=date(9999, 12, 31)))])

        # WHEN
        problems = validate(plan)

        # THEN
        self.assertEqual([f"Event 'Forever': it repeats from 2026-08-31 until 9999-12-31; a repeat can span at most "
                          f"{MAX_REPEAT_DAYS} days."], problems)

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
                          "Class 'HIST 200': 1 assignment(s) have no category and go in Uncategorized.",
                          "Class 'HIST 200': no meeting times, so it won't show on the calendar.",
                          "Credits aren't set for 'HIST 200', so they import with 0 credits."], notes)

    def test_repeated_classes_and_events_outside_the_term(self) -> None:
        # GIVEN
        plan = given_a_plan(
            courses=[Course(title="BIO 151", credits=Decimal(4), is_online=True),
                     Course(title="bio 151", credits=Decimal(1), is_online=True)],
            events=[Event(title="Orientation", start_date=date(2026, 8, 20)),
                    Event(title="Review", start_date=date(2026, 12, 1),
                          repeat=Repeat(days=["monday"], until=date(2027, 1, 11)))])

        # WHEN
        notes = warnings(plan)

        # THEN
        self.assertEqual(["Class 'BIO 151' is listed 2 times; each becomes its own class in Helium.",
                          "Event 'Orientation' (on 2026-08-20) falls outside the term (2026-08-31 to 2026-12-18).",
                          "Event 'Review' (2026-12-01 to 2027-01-11) falls outside the term (2026-08-31 to "
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
        self.assertEqual(["Mon/Wed/Fri 10:00-10:50; Mon 14:00-16:50"], bio["meets"])
        self.assertEqual("weighted", bio["grading"])
        self.assertEqual(["Homework (40%)", "Exams (60%)"], bio["categories"])
        self.assertEqual(("2026-09-14", "2026-12-14"), (bio["first_due"], bio["last_due"]))
        self.assertIsNone(summary["classes"][1]["grading"])
        self.assertEqual([{"title": "Office Hours", "time": "15:00-16:00", "first": "2026-09-01",
                           "last": "2026-09-08", "occurrences": 2}], summary["events"])

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
