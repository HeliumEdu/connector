import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

import httpx

from heliumedu.exception import (
    HeliumAuthError,
    HeliumConnectionError,
    HeliumNotFoundError,
    HeliumPermissionError,
    HeliumRateLimitedError,
    HeliumValidationError,
)

#: Where Helium's API is.
DEFAULT_API_URL: Final = "https://api.heliumedu.com"
#: How long one request to Helium may take, in seconds (an import runs synchronously, so allow for a large one).
REQUEST_TIMEOUT: Final = 60
#: How many seconds to wait when Helium asks to slow down without saying for how long.
DEFAULT_RETRY_AFTER: Final = 60
#: The name the import file is uploaded under (Helium picks the parser from the extension).
IMPORT_FILE_NAME: Final = "helium-import.json"
#: The name an ``.ics`` import's file is sent under.
ICS_FILE_NAME: Final = "calendar.ics"


@dataclass(frozen=True)
class ImportResult:
    """
    What Helium said about an import.
    """

    #: How many of each entity were imported (or would be, for a dry run), keyed by import section.
    counts: dict[str, int] = field(default_factory=dict)
    #: The ids Helium assigned, keyed by import section, then by the id the entity had in the file.
    ids: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Whether this was a dry run (validated, then rolled back).
    dry_run: bool = False


@dataclass(frozen=True)
class Page:
    """
    One page of a list Helium returns in pages.
    """

    #: The page's items.
    items: list[dict[str, Any]]
    #: How many items the whole list has.
    total: int
    #: Whether a later page has more.
    has_more: bool


class HeliumClient:
    """
    A connection to one Helium account through a bearer token (an OAuth access token or a session's JWT).
    """

    def __init__(self,
                 session: httpx.AsyncClient,
                 token: str,
                 base_url: str = DEFAULT_API_URL,
                 *,
                 agent: str | None = None) -> None:
        self._session = session
        self._token = token

        #: Where Helium's API is (Helium's own unless pointed at another backend).
        self.base_url: str = base_url.rstrip("/")
        #: The ``User-Agent`` every request sends: this package, then the caller's own product (e.g.
        #: ``helium-connector/1.0.0``) when given.
        self.user_agent: str = "heliumedu" + (f" {agent}" if agent else "")

    async def get_user(self) -> dict[str, Any]:
        """
        Get the token's user, including their settings (``settings.time_zone`` is the time_zone Helium shows them in).

        :return: The user, as ``GET /auth/user/`` returns it.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the token or can't be reached.
        """
        return await self._get_object("/auth/user/")

    async def import_data(self,
                          data: dict[str, Any],
                          *,
                          dry_run: bool = False,
                          course_group_id: int | None = None) -> ImportResult:
        """
        Import a file's worth of data into the account, all or nothing. Entities reference each other by ids that
        only need to be unique within ``data``; Helium assigns new ones.

        :param data: The import, shaped like ``GET /importexport/export/``.
        :param dry_run: Validate the import without keeping it.
        :param course_group_id: An existing class group to import every class into, in place of ``data``'s class
                                groups (which aren't imported).
        :return: What was imported.
        :raises: :class:`~heliumedu.exception.HeliumValidationError`: When Helium rejects the data.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        files = {"file[]": (IMPORT_FILE_NAME, json.dumps(data).encode("utf-8"), "application/json")}
        params = {"dry_run": "true"} if dry_run else {}
        if course_group_id is not None:
            params["course_group"] = str(course_group_id)
        body = await self._request("POST", "/importexport/import/", params=params, files=files)

        if not isinstance(body, dict):
            return ImportResult(dry_run=dry_run)
        ids = body.pop("ids", None)
        counts = {section: count for section, count in body.items() if isinstance(count, int)}
        return ImportResult(counts, ids if isinstance(ids, dict) else {}, dry_run)

    async def import_ics(self, ics: str, *, course_id: int | None = None, dry_run: bool = False) -> ImportResult:
        """
        Import an ``.ics`` calendar's entries, all or nothing: as assignments in an existing class, or as events. A
        calendar carries only titles, dates, and times, so the assignments are ungraded and uncategorized.

        :param ics: The calendar, as the ``.ics`` file's text.
        :param course_id: The class its entries become assignments in; unset makes them events.
        :param dry_run: Validate the import without keeping it.
        :return: What was imported.
        :raises: :class:`~heliumedu.exception.HeliumValidationError`: When Helium rejects the calendar or the class.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        files = {"file[]": (ICS_FILE_NAME, ics.encode("utf-8"), "text/calendar")}
        data = {"target_type": "events"} if course_id is None else {"target_type": "course", "course": str(course_id)}
        params = {"dry_run": "true"} if dry_run else None
        body = await self._request("POST", "/importexport/import/ics/", params=params, files=files, data=data)

        if not isinstance(body, dict):
            return ImportResult(dry_run=dry_run)
        counts = {section: count for section, count in body.items() if isinstance(count, int)}
        return ImportResult(counts, {}, dry_run)

    async def list_course_groups(self) -> list[dict[str, Any]]:
        """
        List the account's class groups (terms).

        :return: The class groups, as ``GET /planner/coursegroups/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/coursegroups/")

    async def list_courses(self) -> list[dict[str, Any]]:
        """
        List the account's classes, across all class groups.

        :return: The classes, as ``GET /planner/courses/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/courses/")

    async def list_course_schedules(self) -> list[dict[str, Any]]:
        """
        List the account's class meeting schedules, across all classes.

        :return: The schedules, as ``GET /planner/courseschedules/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/courseschedules/")

    async def list_categories(self) -> list[dict[str, Any]]:
        """
        List the account's grading categories, across all classes.

        :return: The categories, as ``GET /planner/categories/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/categories/")

    async def list_homework(self,
                            start: date | datetime | None = None,
                            end: date | datetime | None = None,
                            *,
                            course_ids: list[int] | None = None,
                            completed: bool | None = None,
                            overdue: bool | None = None,
                            category_ids: list[int] | None = None,
                            search: str | None = None) -> list[dict[str, Any]]:
        """
        List the account's assignments, soonest first. A date (not a datetime) is midnight in the user's time zone.

        :param start: The start of the window (given together with ``end``).
        :param end: The end of the window (given together with ``start``).
        :param course_ids: Only these classes' assignments.
        :param completed: Only completed (``True``) or incomplete (``False``) assignments.
        :param overdue: Only overdue assignments.
        :param category_ids: Only these categories' assignments.
        :param search: Only assignments whose title, category, or class title matches.
        :return: The assignments, as ``GET /planner/homework/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        params = _window(start, end)
        params["ordering"] = "start"
        if course_ids:
            params["course__id"] = ",".join(str(course_id) for course_id in course_ids)
        if completed is not None:
            params["completed"] = _flag(completed)
        if overdue is not None:
            params["overdue"] = _flag(overdue)
        if category_ids:
            params["category__id"] = ",".join(str(category_id) for category_id in category_ids)
        if search:
            params["search"] = search
        return await self._get_list("/planner/homework/", params)

    async def list_events(self,
                          start: date | datetime | None = None,
                          end: date | datetime | None = None,
                          *,
                          search: str | None = None) -> list[dict[str, Any]]:
        """
        List the account's events. A recurring event is returned once, as its series (``recurrence_rule`` and
        ``exception_dates``), if the series starts before the window ends. A date (not a datetime) is midnight in the
        user's time zone.

        :param start: The start of the window (given together with ``end``).
        :param end: The end of the window (given together with ``start``).
        :param search: Only events whose title matches.
        :return: The events, as ``GET /planner/events/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        params = _window(start, end)
        if search:
            params["search"] = search
        return await self._get_list("/planner/events/", params)

    async def get_grades(self) -> dict[str, Any]:
        """
        Get the account's grades: class groups, then classes, then categories, as Helium calculates them.

        :return: The grades, as ``GET /planner/grades/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_object("/planner/grades/")

    async def get_needed_grade(self, course_id: int, target_id: int, desired_grade: float) -> dict[str, Any]:
        """
        Get the score a class needs to reach a desired overall grade, as Helium's grade calculator works it out.

        :param course_id: The class.
        :param target_id: In a weighted class, a category with weight and exactly one ungraded assignment left; in a
            points-based class, one of its ungraded assignments.
        :param desired_grade: The desired overall class grade, 0-100.
        :return: The result, as ``GET /planner/grades/needed/`` returns it.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_object("/planner/grades/needed/", {"course": str(course_id), "target": str(target_id),
                                                                  "desired_grade": str(desired_grade)})

    async def list_notes(self,
                         search: str | None = None,
                         *,
                         homework_id: int | None = None,
                         course_id: int | None = None,
                         event_id: int | None = None,
                         resource_id: int | None = None) -> list[dict[str, Any]]:
        """
        List the account's notes, with their content (a Quill Delta).

        :param search: Only notes whose title, text, or linked item's title matches.
        :param homework_id: Only the note linked to this assignment.
        :param course_id: Only notes linked to this class's assignments or resources.
        :param event_id: Only the note linked to this event.
        :param resource_id: Only the note linked to this resource.
        :return: The notes, as ``GET /planner/notes/?include_content=true`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/notes/",
                                    _notes_params(search, homework_id, course_id, event_id, resource_id))

    async def list_notes_page(self,
                              search: str | None = None,
                              *,
                              homework_id: int | None = None,
                              course_id: int | None = None,
                              event_id: int | None = None,
                              resource_id: int | None = None,
                              page: int = 1,
                              page_size: int = 10) -> Page:
        """
        One page of the account's notes, most recently updated first, with their content (a Quill Delta).

        :param search: Only notes whose title, text, or linked item's title matches.
        :param homework_id: Only the note linked to this assignment.
        :param course_id: Only notes linked to this class's assignments or resources.
        :param event_id: Only the note linked to this event.
        :param resource_id: Only the note linked to this resource.
        :param page: Which page, counting from 1.
        :param page_size: How many notes a page holds (at most 100).
        :return: The page, its notes as ``GET /planner/notes/?include_content=true`` returns them. A page past the
            last is empty.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        params = {**_notes_params(search, homework_id, course_id, event_id, resource_id), "ordering": "-updated_at",
                  "page": str(page), "page_size": str(page_size)}
        try:
            return await self._get_page("/planner/notes/", params)
        except HeliumNotFoundError:
            if page == 1:
                raise
            first = await self._get_page("/planner/notes/", {**params, "page": "1"})
            return Page([], first.total, False)

    async def list_resource_groups(self) -> list[dict[str, Any]]:
        """
        List the account's resource groups (e.g. a term's textbooks).

        :return: The resource groups, as ``GET /planner/resourcegroups/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/resourcegroups/")

    async def list_resources(self) -> list[dict[str, Any]]:
        """
        List the account's resources (textbooks, materials, links), across all resource groups.

        :return: The resources, as ``GET /planner/resources/`` returns them.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the request or can't be reached.
        """
        return await self._get_list("/planner/resources/")

    async def _get_object(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        body = await self._request("GET", path, params=params)
        return body if isinstance(body, dict) else {}

    async def _get_page(self, path: str, params: dict[str, str]) -> Page:
        body = await self._request("GET", path, params=params)
        if not isinstance(body, dict):
            return Page([], 0, False)
        items = body.get("results")
        return Page(items if isinstance(items, list) else [], int(body.get("count") or 0), bool(body.get("next")))

    async def _get_list(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        body = await self._request("GET", path, params=params)
        return body if isinstance(body, list) else []

    async def _request(self,
                       method: str,
                       path: str,
                       *,
                       params: dict[str, str] | None = None,
                       files: dict[str, tuple[str, bytes, str]] | None = None,
                       data: dict[str, str] | None = None) -> Any:
        try:
            response = await self._session.request(method,
                                                   f"{self.base_url}{path}",
                                                   params=params,
                                                   files=files,
                                                   data=data,
                                                   headers={"Authorization": f"Bearer {self._token}",
                                                            "Accept": "application/json",
                                                            "User-Agent": self.user_agent},
                                                   timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as e:
            raise HeliumConnectionError(f"Couldn't reach Helium: {e}") from e

        status = response.status_code
        try:
            body = json.loads(response.text) if response.text else None
        except ValueError:
            body = None
        if 200 <= status < 300:
            return body
        elif status == 400:
            raise HeliumValidationError("Helium rejected the request's data.", {"errors": body})
        elif status == 401:
            raise HeliumAuthError("Helium didn't accept the token.")
        elif status == 403:
            raise HeliumPermissionError("The token wasn't granted access to this.", {"errors": body})
        elif status == 404:
            raise HeliumNotFoundError("Helium couldn't find what was asked for.")
        elif status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                seconds = max(1, int(retry_after)) if retry_after is not None else DEFAULT_RETRY_AFTER
            except ValueError:
                seconds = DEFAULT_RETRY_AFTER
            raise HeliumRateLimitedError("Helium asked for fewer requests.", {"retry_after": seconds})
        elif status >= 500:
            raise HeliumConnectionError(f"Helium answered {status}.")
        else:
            raise HeliumValidationError(f"Helium answered {status}.", {"errors": body})


def _notes_params(search: str | None,
                  homework_id: int | None,
                  course_id: int | None = None,
                  event_id: int | None = None,
                  resource_id: int | None = None) -> dict[str, str]:
    params = {"include_content": "true"}
    if search:
        params["search"] = search
    for name, value in (("homework", homework_id), ("course", course_id), ("event", event_id),
                        ("resource", resource_id)):
        if value is not None:
            params[name] = str(value)
    return params


def _window(start: date | datetime | None, end: date | datetime | None) -> dict[str, str]:
    if (start is None) != (end is None):
        raise ValueError("start and end must be given together")
    return {"from": start.isoformat(), "to": end.isoformat()} if start is not None and end is not None else {}


def _flag(value: bool) -> str:
    return "true" if value else "false"
