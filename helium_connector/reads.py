import json
import math
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse
from dateutil.rrule import rrulestr

from helium_connector.models import WEEKDAYS

#: The most characters of one note's text a result carries.
MAX_NOTE_CHARS: Final = 4000
#: Helium's sentinel for "no grade yet".
NO_GRADE: Final = -1
#: A resource's ``status``, by value.
RESOURCE_STATUSES: Final = ("Owned", "Rented", "Ordered", "Shipped", "Need", "Received", "To Sell", "Digital")
#: A resource's ``condition``, by value.
RESOURCE_CONDITIONS: Final = ("Brand New", "Refurbished", "Used - Like New", "Used - Very Good", "Used - Good",
                              "Used - Acceptable", "Used - Poor", "Broken", "Digital")


def shape_classes(course_groups: list[dict[str, Any]],
                  courses: list[dict[str, Any]],
                  schedules: list[dict[str, Any]],
                  categories: list[dict[str, Any]],
                  today: date,
                  include_past: bool) -> list[dict[str, Any]]:
    """
    Nest classes, meeting times, and categories under their terms.

    :param course_groups: ``GET /planner/coursegroups/``.
    :param courses: ``GET /planner/courses/``.
    :param schedules: ``GET /planner/courseschedules/``.
    :param categories: ``GET /planner/categories/``.
    :param today: Today, in the student's time zone.
    :param include_past: Include terms that have ended.
    :return: The terms, soonest-ending last.
    """
    terms = []
    for group in sorted(course_groups, key=lambda group: group["start_date"]):
        if not include_past and date.fromisoformat(group["end_date"]) < today:
            continue
        terms.append({
            "id": group["id"],
            "title": group["title"],
            "start_date": group["start_date"],
            "end_date": group["end_date"],
            "no_class_dates": _no_class_dates(group.get("exceptions")),
            "overall_grade": _grade(group.get("overall_grade")),
            "classes": [_shape_class(course, schedules, categories) for course in courses
                        if course.get("course_group") == group["id"]],
        })
    return terms


def shape_assignments(homework: list[dict[str, Any]],
                      courses: list[dict[str, Any]],
                      categories: list[dict[str, Any]],
                      course_groups: list[dict[str, Any]],
                      time_zone: ZoneInfo) -> list[dict[str, Any]]:
    """
    Trim assignments to what a student asks about, with times in their time_zone.

    :param homework: ``GET /planner/homework/``.
    :param courses: ``GET /planner/courses/``, to name each assignment's class.
    :param categories: ``GET /planner/categories/``, to name each assignment's category.
    :param course_groups: ``GET /planner/coursegroups/``, to name each assignment's term.
    :param time_zone: The student's time zone.
    :return: The assignments.
    """
    courses_by_id = {course["id"]: course for course in courses}
    term_titles = {group["id"]: group["title"] for group in course_groups}
    category_titles = {category["id"]: category["title"] for category in categories}
    return [{
        "id": item["id"],
        "title": item["title"],
        "class": courses_by_id.get(item.get("course"), {}).get("title"),
        "class_id": item.get("course"),
        "term": term_titles.get(courses_by_id.get(item.get("course"), {}).get("course_group")),
        "category": category_titles.get(item.get("category")),
        "due": (isoparse(item["start"]).astimezone(time_zone).date().isoformat() if item.get("all_day")
                else _local(item["start"], time_zone)),
        "end": _local(item["end"], time_zone) if item.get("show_end_time") and not item.get("all_day") else None,
        "all_day": bool(item.get("all_day")),
        "completed": bool(item.get("completed")),
        "grade": (None if not item.get("current_grade") or str(item["current_grade"]).startswith(f"{NO_GRADE}/")
                  else str(item["current_grade"])),
        "priority": item.get("priority"),
    } for item in homework]


def due_between(assignments: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    """
    Keep the assignments due on a local day in a window (Helium matches a window in UTC, so an all-day item can
    otherwise land a day early or late).

    :param assignments: Assignments from :func:`shape_assignments`.
    :param start: The window's first day.
    :param end: The window's last day.
    :return: The assignments due in the window.
    """
    return [assignment for assignment in assignments if start <= date.fromisoformat(assignment["due"][:10]) <= end]


def expand_events(events: list[dict[str, Any]], start: date, end: date, time_zone: ZoneInfo) -> list[dict[str, Any]]:
    """
    List each event occurrence inside a window, expanding recurring series in the student's time zone.

    :param events: ``GET /planner/events/``.
    :param start: The window's first day.
    :param end: The window's last day.
    :param time_zone: The student's time zone.
    :return: The occurrences, in order.
    """
    window_start = datetime.combine(start, time(0), time_zone)
    window_end = datetime.combine(end + timedelta(days=1), time(0), time_zone)
    occurrences = []
    for event in events:
        begins = isoparse(event["start"]).astimezone(time_zone)
        duration = isoparse(event["end"]) - isoparse(event["start"])
        for occurrence in _event_starts(event, begins, window_start, window_end):
            if occurrence < window_end and (occurrence >= window_start or occurrence + duration > window_start):
                occurrences.append({
                    "id": event["id"],
                    "title": event["title"],
                    "start": occurrence.date().isoformat() if event.get("all_day") else occurrence.isoformat(),
                    "end": None if event.get("all_day") else (occurrence + duration).isoformat(),
                    "all_day": bool(event.get("all_day")),
                    "location": event.get("location") or None,
                    "priority": event.get("priority"),
                    "repeats": bool(event.get("recurrence_rule")),
                })
    return sorted(occurrences, key=lambda occurrence: occurrence["start"])


def shape_grades(grades: dict[str, Any], term_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """
    Trim Helium's grade tree to terms, classes, and categories (grades as percentages, ``None`` when nothing is
    graded yet; trends as the app's arrows: up, down, or steady).

    :param grades: ``GET /planner/grades/``.
    :param term_ids: Only these terms, if given.
    :return: The terms.
    """
    return [{
        "id": group["id"],
        "title": group["title"],
        "overall_grade": _grade(group.get("overall_grade")),
        "trend": _trend(group.get("trend")),
        "classes": [{
            "id": course["id"],
            "title": course["title"],
            "overall_grade": _grade(course.get("overall_grade")),
            "trend": _trend(course.get("trend")),
            "weighted": bool(course.get("has_weighted_grading")),
            "graded_assignments": course.get("num_homework_graded"),
            "assignments": course.get("num_homework"),
            "points_earned": course.get("points_earned"),
            "points_possible": course.get("points_possible"),
            "categories": [{
                "id": category["id"],
                "title": category["title"],
                "weight": category.get("weight"),
                "average_grade": _grade(category.get("overall_grade")),
                "trend": _trend(category.get("trend")),
                "graded_assignments": category.get("num_homework_graded"),
                "ungraded_assignments": category.get("num_homework", 0) - category.get("num_homework_graded", 0),
            } for category in course.get("categories", [])],
        } for course in group.get("courses", [])],
    } for group in grades.get("course_groups", []) if term_ids is None or group["id"] in term_ids]


def shape_needed_grade(needed: dict[str, Any]) -> dict[str, Any]:
    """
    Trim Helium's "What Do I Need?" result, rounded as the app shows it.

    :param needed: ``GET /planner/grades/needed/``.
    :return: The result.
    """
    assignment = needed.get("assignment")
    category = needed.get("category")
    return {
        "outcome": needed["state"],
        "needed_grade": _as_shown(needed["needed_grade"]),
        "category": category["title"] if category else None,
        "assignment": {
            "title": assignment["title"],
            "points_possible": assignment["points_possible"],
            "needed_points": _as_shown(assignment["needed_points"]),
        } if assignment else None,
        "total_weight": needed.get("total_weight_accounted_for"),
    }


def shape_resources(resource_groups: list[dict[str, Any]],
                    resources: list[dict[str, Any]],
                    courses: list[dict[str, Any]],
                    class_id: int | None = None) -> list[dict[str, Any]]:
    """
    Nest resources (textbooks, materials, links) under their groups, naming the classes each is for.

    :param resource_groups: ``GET /planner/resourcegroups/``.
    :param resources: ``GET /planner/resources/``.
    :param courses: ``GET /planner/courses/``, to name each resource's classes.
    :param class_id: Only resources for this class, if given.
    :return: The groups that have resources left, each with its resources.
    """
    course_titles = {course["id"]: course["title"] for course in courses}
    groups = []
    for group in resource_groups:
        listed = [{
            "id": resource["id"],
            "title": resource["title"],
            "status": _label(RESOURCE_STATUSES, resource.get("status")),
            "condition": _label(RESOURCE_CONDITIONS, resource.get("condition")),
            "website": resource.get("website") or None,
            "price": resource.get("price") or None,
            "classes": [{"id": course_id, "title": course_titles[course_id]}
                        for course_id in resource.get("courses") or [] if course_id in course_titles],
        } for resource in resources if resource.get("resource_group") == group["id"]
            and (class_id is None or class_id in (resource.get("courses") or []))]
        if listed or class_id is None:
            groups.append({"id": group["id"], "title": group["title"], "resources": listed})
    return groups


def shape_notes(notes: list[dict[str, Any]],
                courses: list[dict[str, Any]],
                time_zone: ZoneInfo,
                limit: int) -> list[dict[str, Any]]:
    """
    Trim notes to their title, what they're linked to (and its class), and plain text.

    :param notes: ``GET /planner/notes/?include_content=true``.
    :param courses: ``GET /planner/courses/``, to name each note's class.
    :param time_zone: The student's time zone.
    :param limit: The most notes to return, most recently updated first.
    :return: The notes.
    """
    recent = sorted(notes, key=lambda note: note.get("updated_at") or "", reverse=True)[:limit]
    course_titles = {course["id"]: course["title"] for course in courses}
    return [{
        "id": note["id"],
        "title": note.get("title") or None,
        "linked_to": note.get("linked_entity_title"),
        "linked_type": note.get("linked_entity_type"),
        "class": course_titles.get(note.get("course_id")),
        "updated": _local(note["updated_at"], time_zone) if note.get("updated_at") else None,
        "text": delta_to_text(note.get("content"))[:MAX_NOTE_CHARS],
    } for note in recent]


def delta_to_text(content: Any) -> str:
    """
    The plain text of a note's Quill Delta (embeds such as images are dropped).

    :param content: The Delta, as a dict or JSON string.
    :return: The text.
    """
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return str(content)
    ops = content.get("ops", []) if isinstance(content, dict) else []
    return "".join(op["insert"] for op in ops if isinstance(op, dict) and isinstance(op.get("insert"), str)).strip()


def _shape_class(course: dict[str, Any],
                 schedules: list[dict[str, Any]],
                 categories: list[dict[str, Any]]) -> dict[str, Any]:
    course_categories = [category for category in categories if category.get("course") == course["id"]]
    return {
        "id": course["id"],
        "title": course["title"],
        "room": course.get("room") or None,
        "is_online": bool(course.get("is_online")),
        "teacher_name": course.get("teacher_name") or None,
        "teacher_email": course.get("teacher_email") or None,
        "website": course.get("website") or None,
        "credits": course.get("credits"),
        "start_date": course.get("start_date"),
        "end_date": course.get("end_date"),
        "no_class_dates": _no_class_dates(course.get("exceptions")),
        "current_grade": _grade(course.get("current_grade")),
        "meets": [_describe_schedule(schedule) for schedule in schedules if schedule.get("course") == course["id"]],
        "grading": (None if not course_categories
                    else "points-based" if all(float(category.get("weight") or 0) == 0
                                               for category in course_categories)
                    else "weighted"),
        "categories": [{"id": category["id"], "title": category["title"], "weight": category.get("weight")}
                       for category in course_categories],
    }


def _describe_schedule(schedule: dict[str, Any]) -> str:
    cycle_length = schedule.get("cycle_length")
    if cycle_length:
        anchor = schedule.get("anchor_date")
        cycle = (f"A/B Day (A Day on {anchor})" if cycle_length == 2
                 else f"{cycle_length}-day cycle (Day 1 on {anchor})")
        slots = []
        for slot in schedule.get("cycle_slots") or []:
            indices = slot["indices"]
            if cycle_length == 2:
                days = "A and B Days" if len(set(indices)) == 2 else ("A Day" if indices[0] == 1 else "B Day")
            else:
                days = describe_numbered_days(indices)
            slots.append(f"{days} {slot['start_time'][:5]}-{slot['end_time'][:5]}")
        description = f"{cycle}: {'; '.join(slots)}"
    else:
        by_time: dict[str, list[str]] = {}
        for index, day in enumerate(WEEKDAYS):
            if schedule.get("days_of_week", "0000000")[index] == "1":
                times = f"{schedule[f'{day[:3]}_start_time'][:5]}-{schedule[f'{day[:3]}_end_time'][:5]}"
                by_time.setdefault(times, []).append(day[:3].capitalize())
        description = "; ".join(f"{'/'.join(days)} {times}" for times, days in by_time.items())
        if schedule.get("is_week_based"):
            week = "A" if schedule.get("week_offset") == 0 else "B"
            description = f"Week {week} (Week A includes {schedule.get('anchor_date')}): {description}"

    start_date, end_date = schedule.get("start_date"), schedule.get("end_date")
    if start_date and end_date:
        return f"{description}, {start_date} to {end_date}"
    elif start_date:
        return f"{description}, from {start_date}"
    elif end_date:
        return f"{description}, until {end_date}"
    return description


def _no_class_dates(exceptions: str | None) -> list[str]:
    return [date(int(day[:4]), int(day[4:6]), int(day[6:])).isoformat()
            for day in (exceptions or "").split(",") if day.strip()]


def describe_numbered_days(days: list[int]) -> str:
    """
    Cycle days as a student reads them: "Day 3", "Days 1 and 4", "Days 1, 3, and 5".

    :param days: The cycle days.
    :return: The description.
    """
    if len(days) == 1:
        return f"Day {days[0]}"
    if len(days) == 2:
        return f"Days {days[0]} and {days[1]}"
    return f"Days {', '.join(map(str, days[:-1]))}, and {days[-1]}"


def _event_starts(event: dict[str, Any],
                  begins: datetime,
                  window_start: datetime,
                  window_end: datetime) -> list[datetime]:
    rule = event.get("recurrence_rule")
    if not rule:
        return [begins]
    series = rrulestr(rule, dtstart=begins, forceset=True)
    for exception in event.get("exception_dates") or []:
        series.exdate(isoparse(exception).astimezone(begins.tzinfo))
    return list(series.between(window_start - timedelta(days=1), window_end, inc=True))


def _local(value: str, time_zone: ZoneInfo) -> str:
    return isoparse(value).astimezone(time_zone).isoformat()


def _label(labels: tuple[str, ...], value: Any) -> str | None:
    return labels[value] if isinstance(value, int) and 0 <= value < len(labels) else None


def _as_shown(value: float) -> float:
    """
    Round to one decimal as the app displays it: ``intl``'s ``NumberFormat`` rounds the fraction alone, in tenths,
    with a tie away from zero.
    """
    whole = math.floor(abs(value))
    tenths = Decimal((abs(value) - whole) * 10).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return math.copysign(whole + float(tenths) / 10, value)


def _trend(value: Any) -> str | None:
    if value is None:
        return None
    trend = float(value)
    return "up" if trend > 0 else "down" if trend < 0 else "steady"


def _grade(value: Any) -> float | None:
    if value is None:
        return None
    grade = float(value)
    return None if grade == NO_GRADE else round(grade, 2)
