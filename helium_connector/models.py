from datetime import date, time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Weekday = Literal["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
Title = Annotated[str, Field(min_length=1, max_length=255)]

#: Weekdays in the order Helium's ``days_of_week`` string uses (Sunday first).
WEEKDAYS: tuple[Weekday, ...] = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_by_name=True,
                              validate_by_alias=True, serialize_by_alias=True)


class Meeting(_Input):
    """
    When a class meets each week.
    """

    days: list[Weekday] = Field(min_length=1, max_length=7,
                                description="The weekdays the class meets at these times.")
    start_time: time = Field(description="Local start time.")
    end_time: time = Field(description="Local end time.")


class CycleSlot(_Input):
    """
    When a class meets on some days of a day cycle.
    """

    cycle_days: list[int] = Field(min_length=1, max_length=20,
                                  description="The cycle days the class meets at these times, counting from 1 (on "
                                              "an A/B Day cycle, 1 is A and 2 is B).")
    start_time: time = Field(description="Local start time.")
    end_time: time = Field(description="Local end time.")


class _Schedule(_Input):
    start_date: date | None = Field(default=None,
                                    description="The schedule's first day, if it starts after the class does.")
    end_date: date | None = Field(default=None,
                                  description="The schedule's last day, if it ends before the class does.")


class WeeklySchedule(_Schedule):
    """
    A class that meets on the same days every week.
    """

    pattern: Literal["weekly"]
    meetings: list[Meeting] = Field(min_length=1, max_length=20)


class WeekRotationSchedule(_Schedule):
    """
    A class that meets every other week (Week A/B, odd/even, or fortnightly weeks).
    """

    pattern: Literal["week_rotation"]
    week: Literal["A", "B"] = Field(description="The week of the rotation this schedule meets.")
    week_a_starts_on: date = Field(description="A date in a Week A; the rotation alternates from that week.")
    meetings: list[Meeting] = Field(min_length=1, max_length=20)


class DayCycleSchedule(_Schedule):
    """
    A class on a rotating cycle of school days (A/B Day, or a numbered 6-day, 7-day, 8-day, or 10-day cycle).
    """

    pattern: Literal["day_cycle"]
    cycle_length: int = Field(ge=2, le=20,
                              description="School days in one cycle (2 for A/B Day). The cycle advances one day per "
                                          "school day, Monday to Friday, skipping no-class dates.")
    day_one: date = Field(description="A school day within the class's dates that is cycle Day 1 (an A Day, on an "
                                      "A/B Day cycle). The cycle counts school days from it.")
    slots: list[CycleSlot] = Field(min_length=1, max_length=20)


Schedule = Annotated[WeeklySchedule | WeekRotationSchedule | DayCycleSchedule, Field(discriminator="pattern")]


class Category(_Input):
    """
    A grading category, as the syllabus weighs it.
    """

    title: Title
    weight: Decimal = Field(default=Decimal(0), ge=0, le=100, decimal_places=2,
                            description="Percent of the class grade. 0 for every category means points-based "
                                        "grading.")


class Homework(_Input):
    """
    One graded item: homework, a quiz, an exam, a project.
    """

    model_config = ConfigDict(title="Assignment")

    title: Title
    category: str | None = Field(default=None,
                                 description="The title of one of this class's categories. Unset puts it in "
                                             "Uncategorized.")
    due_date: date = Field(description="Local due date (an exam's date).")
    due_time: time | None = Field(default=None,
                                  description="Local due time (an exam's start). Unset makes it an all-day item.")
    end_time: time | None = Field(default=None, description="Local end time, for items that span time (exams).")


class Course(_Input):
    """
    One class, from its syllabus.
    """

    model_config = ConfigDict(title="Class")

    title: Title
    credits: Decimal | None = Field(default=None, ge=0, le=99, decimal_places=2,
                                    description="Credit hours. Unset when the syllabus doesn't say.")
    room: str | None = Field(default=None, max_length=255)
    is_online: bool = False
    teacher_name: str | None = Field(default=None, max_length=255)
    teacher_email: str | None = Field(default=None, max_length=254)
    website: str | None = Field(default=None, max_length=3000)
    start_date: date | None = Field(default=None, description="The class's first day, if not the term's.")
    end_date: date | None = Field(default=None, description="The class's last day, if not the term's.")
    no_class_dates: list[date] = Field(default_factory=list, max_length=366,
                                       description="Days this class doesn't meet, beyond the term's.")
    schedules: list[Schedule] = Field(default_factory=list, max_length=20,
                                      description="When the class meets. A class can have several (a lecture and a "
                                                  "lab).")
    categories: list[Category] = Field(default_factory=list, max_length=50)
    homework: list[Homework] = Field(default_factory=list, max_length=1000, alias="assignments")


class Repeat(_Input):
    """
    How an event repeats: weekly, on these days, through a date. Each occurrence becomes its own event.
    """

    days: list[Weekday] = Field(min_length=1, max_length=7)
    until: date = Field(description="The last day an occurrence can fall on.")


class Event(_Input):
    """
    An ungraded calendar item (office hours, a review session).
    """

    title: Title
    start_date: date = Field(description="Local date of the event (the first occurrence, if it repeats).")
    start_time: time | None = Field(default=None, description="Local start time. Unset makes it all-day.")
    end_time: time | None = Field(default=None, description="Local end time.")
    repeat: Repeat | None = Field(default=None,
                                  description="Repeats weekly. Occurrences on the term's no-class dates are "
                                              "skipped.")


class CourseGroup(_Input):
    """
    The term the classes belong to (a Class Group in Helium).
    """

    model_config = ConfigDict(title="Term")

    title: Title
    start_date: date
    end_date: date
    no_class_dates: list[date] = Field(default_factory=list, max_length=366,
                                       description="Days no class meets (holidays, breaks).")


class TermPlan(_Input):
    """
    A term's assignments, events, classes, schedules, and grading, read from syllabi. Dates and times are local: Helium
    places them in the time zone set in the student's Helium account.
    """

    course_group: CourseGroup = Field(alias="term")
    courses: list[Course] = Field(min_length=1, max_length=50, alias="classes")
    events: list[Event] = Field(default_factory=list, max_length=500)
