import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import islice
from typing import Any, Final
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

from helium_connector.models import WEEKDAYS, Course, CourseGroup, CycleSlot, DayCycleSchedule, Event, Homework, \
    Meeting, Schedule, TermPlan, Weekday, WeekRotationSchedule
from helium_connector.platform_enums import RECURRENCE_HORIZON_YEARS, UNGRADED
from helium_connector.reads import describe_numbered_days, occurs_within_horizon

MAX_ASSIGNMENTS: Final = 1000
MAX_EVENTS: Final = 500

_MAX_EXPANDED_OCCURRENCES = max(MAX_ASSIGNMENTS, MAX_EVENTS) + 1
_ROW_ERROR = re.compile(r"^(?P<section>\w+) id (?P<row_id>\d+), (?P<field>\w+): (?P<message>.+)$", re.DOTALL)
_SECTION_NAMES = {"course_groups": "terms", "courses": "classes", "course_schedules": "schedules",
                  "categories": "categories", "homework": "assignments", "events": "events"}
_SHORT_DAYS = {"sunday": "Sun", "monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu",
               "friday": "Fri", "saturday": "Sat"}
_ICAL_SHORT_DAYS = {day[:2].upper(): short for day, short in _SHORT_DAYS.items()}
_FREQUENCIES = {"DAILY": ("daily", "days"), "WEEKLY": ("weekly", "weeks"), "MONTHLY": ("monthly", "months"),
                "YEARLY": ("yearly", "years")}
_LOCAL_UNTIL = re.compile(r"^\d{8}$")


@dataclass(frozen=True)
class ImportPayload:
    """
    A plan converted to Helium's import format.
    """

    #: The import file's contents, shaped like ``GET /importexport/export/``.
    data: dict[str, Any]
    #: What each row is, for the student, keyed by ``(section, id)``, so Helium's row errors can name it.
    labels: dict[tuple[str, int], str] = field(default_factory=dict)


def validate(plan: TermPlan) -> list[str]:
    """
    Check a plan against what Helium accepts, before anything is sent.

    :param plan: The plan.
    :return: Every problem found, each naming what it's about and what to fix (empty when there are none).
    """
    problems: list[str] = []
    course_group = plan.course_group

    for course in plan.courses:
        problems += _validate_course(course, plan)
    for event in plan.events:
        problems += _validate_event(event, course_group)

    dues = (day for course in plan.courses for homework in course.homework
            for day in occurrences(homework, _no_class_dates(course, plan), _course_dates(course, plan)[1]))
    if sum(1 for _ in islice(dues, MAX_ASSIGNMENTS + 1)) > MAX_ASSIGNMENTS:
        problems.append(f"The plan has more than {MAX_ASSIGNMENTS} assignments, counting each repeating occurrence; "
                        f"the connector imports at most {MAX_ASSIGNMENTS} at once.")
    expanded = (day for event in plan.events
                for day in occurrences(event, course_group.no_class_dates, course_group.end_date))
    if sum(1 for _ in islice(expanded, MAX_EVENTS + 1)) > MAX_EVENTS:
        problems.append(f"The plan has more than {MAX_EVENTS} events, counting each repeating occurrence; "
                        f"the connector imports at most {MAX_EVENTS} at once.")

    return problems


def warnings(plan: TermPlan) -> list[str]:
    """
    Things worth confirming with the student that don't stop an import.

    :param plan: The plan (it may also have problems).
    :return: The warnings.
    """
    notes: list[str] = _repeated_classes(plan)
    course_group = plan.course_group
    for course in plan.courses:
        weights = sum((category.weight for category in course.categories), Decimal(0))
        if course.categories and 0 < weights < 100:
            notes.append(f"Class '{course.title}': category weights add up to {weights}%, not 100%.")
        uncategorized = [homework for homework in course.homework if homework.category is None]
        if course.categories and uncategorized:
            notes.append(f"Class '{course.title}': {len(uncategorized)} assignment(s) have no category and go in "
                         f"Uncategorized.")

    for event in plan.events:
        days = occurrences(event, (), course_group.end_date)
        last = days[-1] if days else event.start_date
        if event.start_date < course_group.start_date or last > course_group.end_date:
            dates = f"on {event.start_date}" if last == event.start_date else f"{event.start_date} to {last}"
            notes.append(f"Event '{event.title}' ({dates}) falls outside the term "
                         f"({course_group.start_date} to {course_group.end_date}).")
    return notes


def to_confirm(plan: TermPlan) -> list[str]:
    """
    What the plan leaves unset that syllabi often omit and the student usually knows.

    :param plan: The plan (it may also have problems).
    :return: Each gap, naming the class and what it imports as.
    """
    gaps: list[str] = []
    for course in plan.courses:
        name = f"Class '{course.title}'"
        if course.credits is None:
            gaps.append(f"{name}: credits aren't set, so it imports with 0 credits and isn't counted in GPA.")
        if not course.schedules and not course.is_online:
            gaps.append(f"{name}: no meeting times, so it won't show on the calendar.")
        if course.schedules and not course.is_online and not course.room:
            gaps.append(f"{name}: no room, and it isn't marked online.")
        if not course.categories:
            gaps.append(f"{name}: no grading categories, so its assignments go in Uncategorized.")
        if not course.homework:
            gaps.append(f"{name}: no assignments.")
        untimed = [homework for homework in course.homework if homework.due_time is None]
        if untimed:
            gaps.append(f"{name}: {len(untimed)} assignment(s) have no due time, so they're all-day.")
    return gaps


def existing_term(course_group: dict[str, Any]) -> CourseGroup:
    """
    A term already in Helium, as a plan's term, so classes added to it are checked against its dates.

    :param course_group: The class group, as ``GET /planner/coursegroups/`` returns it.
    :return: The term.
    """
    no_class_dates = [datetime.strptime(day, "%Y%m%d").date()
                      for day in (course_group.get("exceptions") or "").split(",") if re.fullmatch(r"\d{8}", day)]
    return CourseGroup.model_construct(title=course_group["title"],
                                       start_date=date.fromisoformat(course_group["start_date"]),
                                       end_date=date.fromisoformat(course_group["end_date"]),
                                       no_class_dates=no_class_dates)


def summarize(plan: TermPlan) -> dict[str, Any]:
    """
    Describe what a plan creates, for the student to confirm.

    :param plan: The plan.
    :return: The term, each class, and the totals.
    """
    courses = []
    for course in plan.courses:
        skip, last_day = _no_class_dates(course, plan), _course_dates(course, plan)[1]
        due_days = [(homework, occurrences(homework, skip, last_day)) for homework in course.homework]
        dues = sorted(day for _, days in due_days for day in days)
        points_based = bool(course.categories) and all(category.weight == 0 for category in course.categories)
        courses.append({
            "title": course.title,
            "meets": [_describe_schedule(schedule) for schedule in course.schedules],
            "grading": None if not course.categories else "points-based" if points_based else "weighted",
            "categories": [category.title if points_based else f"{category.title} ({category.weight}%)"
                           for category in course.categories],
            "assignments": len(dues),
            "repeating": [{"title": homework.title, "repeats": describe_rule(homework.recurrence_rule),
                           "assignments": len(days)}
                          for homework, days in due_days if homework.recurrence_rule],
            "first_due": dues[0].isoformat() if dues else None,
            "last_due": dues[-1].isoformat() if dues else None,
        })
    listed_events = []
    for event in plan.events:
        days = occurrences(event, plan.course_group.no_class_dates, plan.course_group.end_date)
        listed_events.append({
            "title": event.title,
            "time": f"{event.start_time:%H:%M}" + (f"-{event.end_time:%H:%M}" if event.end_time else "")
            if event.start_time else "all day",
            "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None,
            "occurrences": len(days),
            "repeats": describe_rule(event.recurrence_rule) if event.recurrence_rule else None,
        })
    return {
        "term": {"title": plan.course_group.title, "start_date": plan.course_group.start_date.isoformat(),
                 "end_date": plan.course_group.end_date.isoformat(),
                 "no_class_dates": len(plan.course_group.no_class_dates)},
        "classes": courses,
        "events": listed_events,
        "totals": {"classes": len(plan.courses),
                   "categories": sum(len(course.categories) for course in plan.courses),
                   "assignments": sum(course["assignments"] for course in courses),
                   "events": sum(event["occurrences"] for event in listed_events)},
    }


def build_import(plan: TermPlan, time_zone: ZoneInfo) -> ImportPayload:
    """
    Convert a valid plan to Helium's import format: ids assigned, schedules packed, local times made timezone-aware
    in the student's time zone. A repeating event or assignment is one row anchored on its first occurrence, its
    ``recurrence_rule`` given a UTC ``UNTIL`` and its occurrences on no-class dates as ``exception_dates``; Helium
    expands it.

    :param plan: A plan with no problems.
    :param time_zone: The student's time zone (their Helium setting).
    :return: The import.
    """
    course_group = plan.course_group
    labels: dict[tuple[str, int], str] = {}
    data: dict[str, Any] = {section: [] for section in (
        "external_calendars", "course_groups", "courses", "course_schedules", "categories", "resource_groups",
        "resources", "events", "homework", "reminders", "notes")}

    data["course_groups"].append({"id": 1, "title": course_group.title,
                                  "start_date": course_group.start_date.isoformat(),
                                  "end_date": course_group.end_date.isoformat(), "shown_on_calendar": True,
                                  "exceptions": _exceptions(course_group.no_class_dates)})
    labels[("course_groups", 1)] = f"Term '{course_group.title}'"

    for course_id, course in enumerate(plan.courses, start=1):
        start_date, end_date = _course_dates(course, plan)
        row: dict[str, Any] = {"id": course_id, "course_group": 1, "title": course.title,
                               "credits": f"{course.credits or Decimal(0):.2f}", "is_online": course.is_online,
                               "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                               "exceptions": _exceptions(course.no_class_dates)}
        for optional in ("room", "teacher_name", "teacher_email", "website"):
            if getattr(course, optional):
                row[optional] = getattr(course, optional)
        data["courses"].append(row)
        labels[("courses", course_id)] = f"Class '{course.title}'"

        for schedule in course.schedules:
            for row in _schedule_rows(schedule):
                schedule_id = len(data["course_schedules"]) + 1
                data["course_schedules"].append({"id": schedule_id, "course": course_id, **row})
                labels[("course_schedules", schedule_id)] = (f"Class '{course.title}' schedule "
                                                             f"({_describe_schedule(schedule)})")

        category_ids: dict[str, int] = {}
        for category in course.categories:
            category_id = len(data["categories"]) + 1
            category_ids[category.title.casefold()] = category_id
            data["categories"].append({"id": category_id, "course": course_id, "title": category.title,
                                       "weight": f"{category.weight:.2f}"})
            labels[("categories", category_id)] = f"Category '{category.title}' ({course.title})"

        skip = _no_class_dates(course, plan)
        for homework in course.homework:
            homework_id = len(data["homework"]) + 1
            homework_category = category_ids.get(homework.category.casefold()) if homework.category else None
            data["homework"].append({"id": homework_id, "course": course_id, "category": homework_category,
                                     "title": homework.title, "current_grade": UNGRADED,
                                     **_item_timing(homework, skip, end_date, time_zone)})
            repeats = f" repeating from {homework.due_date.isoformat()}" if homework.recurrence_rule else ""
            labels[("homework", homework_id)] = f"Assignment '{homework.title}' ({course.title}){repeats}"

    for event in plan.events:
        event_id = len(data["events"]) + 1
        data["events"].append({"id": event_id, "title": event.title,
                               **_item_timing(event, set(course_group.no_class_dates), course_group.end_date,
                                              time_zone)})
        when = "repeating from" if event.recurrence_rule else "on"
        labels[("events", event_id)] = f"Event '{event.title}' {when} {event.start_date.isoformat()}"

    return ImportPayload(data, labels)


def describe_import(plan: TermPlan,
                    counts: dict[str, int],
                    ids: dict[str, dict[str, int]],
                    term_id: int | None = None) -> dict[str, Any]:
    """
    What an import created, in the plan's words, with the ids Helium assigned.

    :param plan: The imported plan.
    :param counts: Helium's counts, by import section.
    :param ids: The ids Helium assigned, by import section, then by the id in the import file.
    :param term_id: The term the classes went into, when it was already in Helium.
    :return: The term's id, each class's id, and how many of each thing were created.
    """
    class_ids = ids.get("courses", {})
    return {
        "term_id": term_id or ids.get("course_groups", {}).get("1"),
        "classes": [{"title": course.title, "id": class_ids.get(str(course_id))}
                    for course_id, course in enumerate(plan.courses, start=1)],
        "created": {name: counts[section] for section, name in _SECTION_NAMES.items() if counts.get(section)},
    }


def occurrences(item: Event | Homework, skip: Iterable[date], last_day: date) -> list[date]:
    """
    The dates an event or assignment falls on: its date, or every occurrence of its ``recurrence_rule`` within
    ``RECURRENCE_HORIZON_YEARS`` of that date, at most ``_MAX_EXPANDED_OCCURRENCES`` of them. A rule without ``COUNT``
    or ``UNTIL`` is followed through ``last_day``. A rule that can't be read counts as just the item's date.

    :param item: The event or assignment.
    :param skip: Dates no occurrence falls on (no-class dates).
    :param last_day: The last day an open-ended rule is followed through.
    :return: The dates, in order.
    """
    try:
        days = _rule_dates(item, last_day)
    except _UnreadableRule:
        return [_first_date(item)]
    skipped = set(skip)
    return [day for day in days if day not in skipped] if item.recurrence_rule else days


def describe_rule(rule: str) -> str:
    """
    A short description of a plan's ``recurrence_rule``, e.g. "every 2 weeks on Mon, Wed until 2026-12-11".

    :param rule: The rule, in the plan's local terms.
    :return: The description, or the rule itself when it isn't one the description covers.
    """
    parts = _rule_parts(rule)
    frequency = _FREQUENCIES.get(parts.get("FREQ", ""))
    if frequency is None:
        return rule
    interval = int(parts["INTERVAL"]) if parts.get("INTERVAL", "").isdigit() else 1
    description = frequency[0] if interval == 1 else f"every {interval} {frequency[1]}"
    if parts.get("BYDAY"):
        description += " on " + ", ".join(_ICAL_SHORT_DAYS.get(day, day) for day in parts["BYDAY"].split(","))
    if parts.get("BYMONTHDAY") == "-1":
        description += " on the last day"
    elif parts.get("BYMONTHDAY"):
        description += f" on day {parts['BYMONTHDAY']}"
    if parts.get("COUNT"):
        description += f", {parts['COUNT']} times"
    elif parts.get("UNTIL") and _LOCAL_UNTIL.match(parts["UNTIL"]):
        until = parts["UNTIL"]
        description += f" until {until[:4]}-{until[4:6]}-{until[6:]}"
    return description


class _UnreadableRule(Exception):
    pass


def _first_date(item: Event | Homework) -> date:
    return item.start_date if isinstance(item, Event) else item.due_date


def _first_time(item: Event | Homework) -> time | None:
    return item.start_time if isinstance(item, Event) else item.due_time


def _rule_parts(rule: str) -> dict[str, str]:
    body = rule.strip()
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    return {key.strip().upper(): value.strip().upper() for key, _, value in
            (part.partition("=") for part in body.split(";") if part.strip())}


def _rule_dates(item: Event | Homework, last_day: date) -> list[date]:
    first = _first_date(item)
    if not item.recurrence_rule:
        return [first]

    parts = _rule_parts(item.recurrence_rule)
    if "UNTIL" in parts:
        if not _LOCAL_UNTIL.match(parts["UNTIL"]):
            raise _UnreadableRule("has an `UNTIL` that isn't a local date, like 20261211")
        parts["UNTIL"] += "T235959"
    open_ended = "COUNT" not in parts and "UNTIL" not in parts
    body = ";".join(f"{key}={value}" for key, value in parts.items())
    dtstart = datetime.combine(first, _first_time(item) or time(0))
    try:
        rule = rrulestr(body, dtstart=dtstart)
    except (ValueError, TypeError) as ex:
        raise _UnreadableRule("isn't an iCal RRULE") from ex
    if not occurs_within_horizon(body, dtstart):
        raise _UnreadableRule(f"has no dates within {RECURRENCE_HORIZON_YEARS} years")

    horizon = first + relativedelta(years=RECURRENCE_HORIZON_YEARS)
    days: list[date] = []
    for occurrence in islice(rule, _MAX_EXPANDED_OCCURRENCES):
        if occurrence.date() > horizon or (open_ended and occurrence.date() > last_day):
            break
        days.append(occurrence.date())
    return days


def _platform_rule(rule: str, last_day: date, time_zone: ZoneInfo) -> str:
    parts = _rule_parts(rule)
    if "COUNT" not in parts and "UNTIL" not in parts:
        parts["UNTIL"] = f"{last_day:%Y%m%d}"
    if "UNTIL" in parts:
        until = datetime.combine(datetime.strptime(parts["UNTIL"], "%Y%m%d").date(), time(23, 59, 59), time_zone)
        parts["UNTIL"] = f"{until.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    return ";".join(f"{key}={value}" for key, value in parts.items())


def _item_timing(item: Event | Homework, skip: set[date], last_day: date, time_zone: ZoneInfo) -> dict[str, Any]:
    start = _first_time(item)
    row = _timing(_first_date(item), start, item.end_time, time_zone)
    if not item.recurrence_rule:
        return row

    row["recurrence_rule"] = _platform_rule(item.recurrence_rule, last_day, time_zone)
    exceptions = [_utc(datetime.combine(day, start or time(0), time_zone))
                  for day in _rule_dates(item, last_day) if day in skip]
    if exceptions:
        row["exception_dates"] = exceptions
    return row


def _utc(moment: datetime) -> str:
    return f"{moment.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def _no_class_dates(course: Course, plan: TermPlan) -> set[date]:
    return set(course.no_class_dates) | set(plan.course_group.no_class_dates)


def explain_platform_errors(errors: Any, labels: dict[tuple[str, int], str]) -> list[str]:
    """
    Turn Helium's import errors into messages that name what they're about.

    :param errors: Helium's error body.
    :param labels: The import's row labels.
    :return: The messages.
    """
    messages: list[str] = []
    if isinstance(errors, dict):
        for section, section_errors in errors.items():
            for error in (section_errors if isinstance(section_errors, list) else [section_errors]):
                messages.append(_explain_row_error(str(section), str(error), labels))
    elif isinstance(errors, list):
        messages += [str(error) for error in errors]
    elif errors:
        messages.append(str(errors))
    return messages or ["Helium rejected the import without saying why."]


def _validate_course(course: Course, plan: TermPlan) -> list[str]:
    problems: list[str] = []
    name = f"Class '{course.title}'"
    start_date, end_date = _course_dates(course, plan)

    if start_date < plan.course_group.start_date or end_date > plan.course_group.end_date:
        problems.append(f"{name}: its dates ({start_date} to {end_date}) fall outside the term "
                        f"({plan.course_group.start_date} to {plan.course_group.end_date}).")

    no_class_dates = _no_class_dates(course, plan)
    for schedule in course.schedules:
        problems += _validate_schedule(name, schedule, start_date, end_date, no_class_dates)

    titles: set[str] = set()
    for category in course.categories:
        if category.title.casefold() in titles:
            problems.append(f"{name}: category '{category.title}' is listed more than once.")
        titles.add(category.title.casefold())

    for homework in course.homework:
        problems += _validate_homework(name, homework, titles, start_date, end_date, no_class_dates)

    return problems


def _validate_schedule(name: str,
                       schedule: Schedule,
                       start_date: date,
                       end_date: date,
                       no_class_dates: set[date]) -> list[str]:
    problems: list[str] = []
    item = f"{name}, {_schedule_label(schedule)}"

    schedule_start, schedule_end = schedule.start_date or start_date, schedule.end_date or end_date
    if schedule_start < start_date or schedule_end > end_date:
        problems.append(f"{item}: its dates ({schedule_start} to {schedule_end}) fall outside the class's dates "
                        f"({start_date} to {end_date}).")

    if isinstance(schedule, DayCycleSchedule):
        problems += _validate_day_cycle(item, schedule, start_date, end_date, no_class_dates)
    return problems


def _validate_day_cycle(item: str,
                        schedule: DayCycleSchedule,
                        start_date: date,
                        end_date: date,
                        no_class_dates: set[date]) -> list[str]:
    problems: list[str] = []
    if schedule.day_one.weekday() > 4:
        problems.append(f"{item}: cycle Day 1 ({schedule.day_one}) is a {schedule.day_one:%A}; the cycle only "
                        f"counts school days, Monday to Friday.")
    elif schedule.day_one in no_class_dates:
        problems.append(f"{item}: cycle Day 1 ({schedule.day_one}) is a no-class date.")
    elif not start_date <= schedule.day_one <= end_date:
        problems.append(f"{item}: cycle Day 1 ({schedule.day_one}) is outside the class's dates ({start_date} to "
                        f"{end_date}). Use a Day 1 within the class's dates.")

    for slot in schedule.slots:
        if len(set(slot.cycle_days)) != len(slot.cycle_days):
            problems.append(f"{item}: a time slot lists the same cycle day more than once.")
    return problems


def _validate_homework(name: str,
                       homework: Homework,
                       category_titles: set[str],
                       start_date: date,
                       end_date: date,
                       no_class_dates: set[date]) -> list[str]:
    problems: list[str] = []
    item = f"{name}, assignment '{homework.title}'"
    if homework.category and homework.category.casefold() not in category_titles:
        problems.append(f"{item}: category '{homework.category}' isn't one of the class's categories.")
    if not start_date <= homework.due_date <= end_date:
        problems.append(f"{item}: due {homework.due_date}, outside the class's dates ({start_date} to "
                        f"{end_date}).")
    if homework.end_time is not None and homework.due_time is None:
        problems.append(f"{item}: an end time needs a due time.")
    if homework.recurrence_rule:
        repeat_problems = _validate_repeat(item, homework, no_class_dates, end_date)
        last = occurrences(homework, no_class_dates, end_date)[-1]
        if not repeat_problems and last > end_date:
            repeat_problems.append(f"{item}: its recurrence_rule `{homework.recurrence_rule}` has occurrences through "
                                   f"{last}, outside the class's dates ({start_date} to {end_date}).")
        problems += repeat_problems
    return problems


def _validate_repeat(name: str, item: Event | Homework, skip: set[date], last_day: date) -> list[str]:
    first, rule = _first_date(item), item.recurrence_rule
    try:
        days = _rule_dates(item, last_day)
    except _UnreadableRule as ex:
        return [f"{name}: its recurrence_rule `{rule}` {ex}."]
    if not days or days[0] != first:
        return [f"{name}: {first} isn't an occurrence of its recurrence_rule `{rule}`; the item's date is its first "
                f"occurrence."]
    if all(day in skip for day in days):
        return [f"{name}: every occurrence of its recurrence_rule `{rule}` falls on a no-class date."]
    return []


def _validate_event(event: Event, course_group: CourseGroup) -> list[str]:
    problems: list[str] = []
    name = f"Event '{event.title}'"
    if event.end_time is not None and event.start_time is None:
        problems.append(f"{name}: an end time needs a start time.")
    if event.recurrence_rule:
        problems += _validate_repeat(name, event, set(course_group.no_class_dates), course_group.end_date)
    return problems


def _course_dates(course: Course, plan: TermPlan) -> tuple[date, date]:
    return course.start_date or plan.course_group.start_date, course.end_date or plan.course_group.end_date


def _exceptions(dates: list[date]) -> str:
    return ",".join(sorted({day.strftime("%Y%m%d") for day in dates}))


def _pack_meetings(meetings: list[Meeting]) -> list[dict[Weekday, Meeting]]:
    """
    Group meetings into schedules, each holding at most one time per weekday (a class meeting twice on a day needs a
    second schedule).
    """
    schedules: list[dict[Weekday, Meeting]] = []
    for meeting in meetings:
        for day in dict.fromkeys(meeting.days):
            schedule = next((schedule for schedule in schedules if day not in schedule), None)
            if schedule is None:
                schedule = {}
                schedules.append(schedule)
            schedule[day] = meeting
    return schedules


def _schedule_rows(schedule: Schedule) -> list[dict[str, Any]]:
    """
    Convert a schedule to Helium's schedule rows: more than one when a class meets twice on the same weekday (or
    cycle day), since a row holds one time per day.
    """
    window = {key: getattr(schedule, key).isoformat() for key in ("start_date", "end_date") if getattr(schedule, key)}
    if isinstance(schedule, DayCycleSchedule):
        return [{**_weekly_fields({}), "cycle_length": schedule.cycle_length,
                 "anchor_date": schedule.day_one.isoformat(), "cycle_slots": _cycle_slots(days), **window}
                for days in _pack_cycle_days(schedule.slots)]

    rotation: dict[str, Any] = {}
    if isinstance(schedule, WeekRotationSchedule):
        rotation = {"is_week_based": True, "week_offset": 0 if schedule.week == "A" else 1,
                    "anchor_date": schedule.week_a_starts_on.isoformat()}
    return [{**_weekly_fields(days), **rotation, **window} for days in _pack_meetings(schedule.meetings)]


def _pack_cycle_days(slots: list[CycleSlot]) -> list[dict[int, CycleSlot]]:
    rows: list[dict[int, CycleSlot]] = []
    for slot in slots:
        for day in dict.fromkeys(slot.cycle_days):
            row = next((row for row in rows if day not in row), None)
            if row is None:
                row = {}
                rows.append(row)
            row[day] = slot
    return rows


def _cycle_slots(days: dict[int, CycleSlot]) -> list[dict[str, Any]]:
    by_time: dict[tuple[time, time], list[int]] = {}
    for day, slot in sorted(days.items()):
        by_time.setdefault((slot.start_time, slot.end_time), []).append(day)
    return [{"indices": indices, "start_time": f"{start:%H:%M:%S}", "end_time": f"{end:%H:%M:%S}"}
            for (start, end), indices in by_time.items()]


def _weekly_fields(days: dict[Weekday, Meeting]) -> dict[str, Any]:
    row: dict[str, Any] = {"days_of_week": "".join("1" if day in days else "0" for day in WEEKDAYS)}
    for day in WEEKDAYS:
        meeting = days.get(day)
        row[f"{day[:3]}_start_time"] = f"{meeting.start_time:%H:%M:%S}" if meeting else "00:00:00"
        row[f"{day[:3]}_end_time"] = f"{meeting.end_time:%H:%M:%S}" if meeting else "00:00:00"
    return row


def _timing(day: date, start: time | None, end: time | None, time_zone: ZoneInfo) -> dict[str, Any]:
    if start is None:
        midnight = datetime.combine(day, time(0), time_zone)
        next_midnight = datetime.combine(day + timedelta(days=1), time(0), time_zone)
        return {"all_day": True, "show_end_time": False, "start": midnight.isoformat(),
                "end": next_midnight.isoformat()}
    begins = datetime.combine(day, start, time_zone)
    ends = datetime.combine(day, end, time_zone) if end is not None else begins
    return {"all_day": False, "show_end_time": end is not None, "start": begins.isoformat(), "end": ends.isoformat()}


def _schedule_label(schedule: Schedule) -> str:
    if isinstance(schedule, DayCycleSchedule):
        return "A/B Day schedule" if schedule.cycle_length == 2 else f"{schedule.cycle_length}-day cycle schedule"
    if isinstance(schedule, WeekRotationSchedule):
        return f"Week {schedule.week} schedule"
    return "weekly schedule"


def _describe_schedule(schedule: Schedule) -> str:
    if isinstance(schedule, DayCycleSchedule):
        cycle = (f"A/B Day (A Day on {schedule.day_one})" if schedule.cycle_length == 2
                 else f"{schedule.cycle_length}-day cycle (Day 1 on {schedule.day_one})")
        slots = "; ".join(f"{_describe_cycle_days(schedule, slot.cycle_days)} "
                          f"{slot.start_time:%H:%M}-{slot.end_time:%H:%M}" for slot in schedule.slots)
        description = f"{cycle}: {slots}"
    else:
        meetings = "; ".join(f"{'/'.join(_SHORT_DAYS[day] for day in meeting.days)} "
                             f"{meeting.start_time:%H:%M}-{meeting.end_time:%H:%M}" for meeting in schedule.meetings)
        if isinstance(schedule, WeekRotationSchedule):
            description = f"Week {schedule.week} (Week A includes {schedule.week_a_starts_on}): {meetings}"
        else:
            description = meetings

    if schedule.start_date and schedule.end_date:
        return f"{description}, {schedule.start_date} to {schedule.end_date}"
    elif schedule.start_date:
        return f"{description}, from {schedule.start_date}"
    elif schedule.end_date:
        return f"{description}, until {schedule.end_date}"
    return description


def _repeated_classes(plan: TermPlan) -> list[str]:
    counts = Counter(course.title.casefold() for course in plan.courses)
    notes: list[str] = []
    for course in plan.courses:
        count = counts.pop(course.title.casefold(), 1)
        if count > 1:
            notes.append(f"Class '{course.title}' is listed {count} times; each becomes its own class in Helium.")
    return notes


def _describe_cycle_days(schedule: DayCycleSchedule, cycle_days: list[int]) -> str:
    if schedule.cycle_length == 2:
        return "A and B Days" if len(set(cycle_days)) == 2 else ("A Day" if cycle_days[0] == 1 else "B Day")
    return describe_numbered_days(cycle_days)


def _explain_row_error(section: str, error: str, labels: dict[tuple[str, int], str]) -> str:
    match = _ROW_ERROR.match(error)
    if match:
        label = labels.get((match["section"], int(match["row_id"])))
        if label:
            return f"{label}: {match['field'].replace('_', ' ')}: {match['message']}"
    return f"{section.replace('_', ' ')}: {error}"
