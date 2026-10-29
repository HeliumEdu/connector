import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, ToolAnnotations
from pydantic import Field, ValidationError

from helium_connector import __version__
from helium_connector.auth import AGENT, HeliumTokenVerifier
from helium_connector.models import TermPlan
from helium_connector.planning import build_import, describe_import, explain_platform_errors, summarize, validate, \
    warnings
from helium_connector.reads import due_between, expand_events, shape_assignments, shape_classes, shape_grades, \
    shape_notes, shape_resources
from helium_connector.settings import Settings
from heliumedu.client import HeliumClient
from heliumedu.exception import (
    HeliumAuthError,
    HeliumConnectionError,
    HeliumError,
    HeliumPermissionError,
    HeliumRateLimitedError,
    HeliumValidationError,
)

logger = logging.getLogger(__name__)

#: Where the connector's documentation is.
DOCS_URL: Final = "https://www.heliumedu.com/support/"
#: How many days a read covers when no window is given.
DEFAULT_WINDOW_DAYS: Final = 7
#: The longest window a read covers, in days.
MAX_WINDOW_DAYS: Final = 366
#: How many items a list returns at a time unless asked for more.
DEFAULT_PAGE_SIZE: Final = 25
#: The most items a list returns at a time.
MAX_PAGE_SIZE: Final = 100
#: What the server tells the model it's for.
INSTRUCTIONS: Final = (
    "Helium is a free student planner. These tools import a term's assignments, events, classes, meeting times, and "
    "grading categories into the user's Helium account, and read the user's assignments, events, notes, classes, "
    "resources, and grades. preview_import_term checks a plan without saving anything; import_term saves it as a "
    "new term. Dates and times in a plan are local: Helium places them in the time zone set in the user's Helium "
    "account, which preview_import_term returns."
)

Status = Literal["all", "incomplete", "completed", "overdue"]


def create_server(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    """
    Build the Helium MCP server and its tools.

    :param settings: The connector's configuration.
    :param transport: How requests reach Helium (the network unless given).
    :return: The server.
    """
    verifier = HeliumTokenVerifier(settings.api_url, transport)
    server: MCPServer = HeliumServer(
        name="helium",
        title="Helium",
        instructions=INSTRUCTIONS,
        website_url="https://www.heliumedu.com",
        version=__version__,
        token_verifier=verifier,
        # subscriptions/listen answers with a long-lived stream, which a stateless Lambda answering in JSON can't hold
        subscriptions=False,
        # Strings, so the URLs keep their exact form (an issuer is compared as an exact string)
        auth=AuthSettings.model_validate({"issuer_url": settings.issuer_url,
                                          "resource_server_url": settings.resource_url,
                                          "service_documentation_url": DOCS_URL,
                                          "validate_token_resource": False}),
    )

    @asynccontextmanager
    async def helium() -> AsyncIterator[tuple[HeliumClient, ZoneInfo]]:
        token = get_access_token()
        if token is None:
            raise ToolError("Helium isn't connected. Connect Helium and sign in, then try again.")
        try:
            user = await verifier.user_for(token.token)
            async with httpx.AsyncClient(transport=transport) as session:
                yield HeliumClient(session, token.token, settings.api_url, agent=AGENT), _time_zone(user)
        except HeliumError as e:
            raise _tool_error(e) from e

    async def import_plan(plan: TermPlan, *, dry_run: bool, allow_duplicate_term: bool) -> dict[str, Any]:
        outcome = "ready" if dry_run else "imported"
        problems = validate(plan)
        notes = warnings(plan)
        if problems:
            return {outcome: False, "problems": problems, "warnings": notes}
        async with helium() as (client, time_zone):
            if not allow_duplicate_term and await _existing_term(client, plan):
                return {outcome: False, "warnings": notes,
                        "problems": [f"A term named '{plan.course_group.title}' with these dates is already in "
                                     f"Helium. Set allow_duplicate_term to import a second copy."]}
            payload = build_import(plan, time_zone)
            try:
                result = await client.import_data(payload.data, dry_run=dry_run)
            except HeliumValidationError as e:
                return {outcome: False, "warnings": notes,
                        "problems": explain_platform_errors((e.meta or {}).get("errors"), payload.labels)}
        if dry_run:
            return {"ready": True, "time_zone": time_zone.key, "summary": summarize(plan), "warnings": notes}
        logger.info(f"Imported a term of {len(plan.courses)} class(es)")
        return {"imported": True, **describe_import(plan, result.counts, result.ids), "warnings": notes,
                "summary": summarize(plan), "open_in_helium": f"{settings.app_url}/classes"}

    @server.tool(title="Preview a term import",
                 description="Check a term's assignments, events, classes, meeting times, and grading categories "
                             "against what Helium accepts, without saving anything. Returns every problem to fix, "
                             "or a summary of what import_term would create.",
                 annotations=_reads("Preview a term import"))
    async def preview_import_term(plan: TermPlan,
                                  allow_duplicate_term: bool = Field(
                                      default=False,
                                      description="Preview even if a term with the same title and dates is already "
                                                  "in Helium.")) -> dict[str, Any]:
        return await import_plan(plan, dry_run=True, allow_duplicate_term=allow_duplicate_term)

    @server.tool(title="Import a term",
                 description="Save a term's assignments, events, classes, meeting times, and grading categories to "
                             "Helium as a new term, all at once. Adds to the account; changes nothing already "
                             "there. preview_import_term shows what it would create.",
                 annotations=ToolAnnotations(title="Import a term", read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=False))
    async def import_term(plan: TermPlan,
                          allow_duplicate_term: bool = Field(
                              default=False,
                              description="Import even if a term with the same title and dates is already in "
                                          "Helium.")) -> dict[str, Any]:
        return await import_plan(plan, dry_run=False, allow_duplicate_term=allow_duplicate_term)

    @server.tool(title="List assignments",
                 description="List the user's assignments (homework, quizzes, exams, projects) due in a window, "
                             "with class, term, category, completion, and grade. Defaults to the next "
                             f"{DEFAULT_WINDOW_DAYS} days; for overdue, the past {MAX_WINDOW_DAYS} days; with a "
                             "query, the class's dates (with class_id) or the current terms. Returns limit at a "
                             "time, with total and has_more; pass page for the next.",
                 annotations=_reads("List assignments"))
    async def list_assignments(start_date: date | None = Field(default=None, description="First day (local)."),
                               end_date: date | None = Field(default=None, description="Last day (local)."),
                               class_id: int | None = Field(default=None,
                                                            description="Only this class (an id from list_classes)."),
                               status: Status = Field(default="all"),
                               query: str | None = Field(default=None,
                                                         description="Only assignments whose title, category, or "
                                                                     "class matches this text."),
                               limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
                               page: int = Field(default=1, ge=1)) -> dict[str, Any]:
        async with helium() as (client, time_zone):
            courses = await client.list_courses()
            groups = await client.list_course_groups()
            course = next((course for course in courses if course["id"] == class_id), None)
            start, end = _read_window(time_zone, start_date, end_date, backward=status == "overdue",
                                      searching=bool(query), course=course, course_groups=groups)
            homework = await client.list_homework(start - timedelta(days=1), end + timedelta(days=2),
                                                  course_ids=[class_id] if class_id is not None else None,
                                                  completed={"incomplete": False, "completed": True}.get(status),
                                                  overdue=True if status == "overdue" else None, search=query)
            categories = await client.list_categories()
        assignments = due_between(shape_assignments(homework, courses, categories, groups, time_zone), start, end)
        return {"start_date": start.isoformat(), "end_date": end.isoformat(),
                **_paged("assignments", assignments, limit, page)}

    @server.tool(title="List events",
                 description="List the user's events (ungraded calendar items) in a window, with repeating events "
                             f"listed per occurrence. Defaults to the next {DEFAULT_WINDOW_DAYS} days; with a query, "
                             "the current terms. Returns limit at a time, with total and has_more; pass page "
                             "for the next.",
                 annotations=_reads("List events"))
    async def list_events(start_date: date | None = Field(default=None, description="First day (local)."),
                          end_date: date | None = Field(default=None, description="Last day (local)."),
                          query: str | None = Field(default=None,
                                                    description="Only events whose title matches this text."),
                          limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
                          page: int = Field(default=1, ge=1)) -> dict[str, Any]:
        async with helium() as (client, time_zone):
            searching_terms = bool(query) and start_date is None and end_date is None
            groups = await client.list_course_groups() if searching_terms else []
            start, end = _read_window(time_zone, start_date, end_date, searching=bool(query), course_groups=groups)
            events = await client.list_events(start, end + timedelta(days=1), search=query)
        return {"start_date": start.isoformat(), "end_date": end.isoformat(),
                **_paged("events", expand_events(events, start, end, time_zone), limit, page)}

    @server.tool(title="List notes",
                 description="List the user's notes as plain text: all of them, those matching a query, or the "
                             "note on one assignment. Returns limit at a time, with total and has_more; pass page "
                             "for the next.",
                 annotations=_reads("List notes"))
    async def list_notes(query: str | None = Field(default=None,
                                                   description="Only notes whose title, text, or linked item "
                                                               "matches this text."),
                         assignment_id: int | None = Field(default=None,
                                                           description="Only the note on this assignment (an id "
                                                                       "from list_assignments)."),
                         limit: int = Field(default=5, ge=1, le=10),
                         page: int = Field(default=1, ge=1)) -> dict[str, Any]:
        async with helium() as (client, time_zone):
            notes = await client.list_notes_page(query, homework_id=assignment_id, page=page, page_size=limit)
        return {"total": notes.total, "page": page, "has_more": notes.has_more,
                "notes": shape_notes(notes.items, time_zone, limit)}

    @server.tool(title="List classes",
                 description="List the user's classes by term, with room, teacher, credits, current grade, meeting "
                             "times, and grading categories.",
                 annotations=_reads("List classes"))
    async def list_classes(include_past_terms: bool = Field(default=False,
                                                            description="Include terms that have ended.")
                           ) -> dict[str, Any]:
        async with helium() as (client, time_zone):
            groups = await client.list_course_groups()
            courses = await client.list_courses()
            schedules = await client.list_course_schedules()
            categories = await client.list_categories()
        return {"terms": shape_classes(groups, courses, schedules, categories, _today(time_zone), include_past_terms)}

    @server.tool(title="List resources",
                 description="List the user's resources (textbooks, materials, links) by resource group, with "
                             "status, condition, website, price, and the classes each is for.",
                 annotations=_reads("List resources"))
    async def list_resources(class_id: int | None = Field(default=None,
                                                          description="Only resources for this class (an id from "
                                                                      "list_classes).")) -> dict[str, Any]:
        async with helium() as (client, _):
            groups = await client.list_resource_groups()
            resources = await client.list_resources()
            courses = await client.list_courses()
        return {"resource_groups": shape_resources(groups, resources, courses, class_id)}

    @server.tool(title="Get grades",
                 description="Get the user's grades: each term's classes, with overall grade, weighted or "
                             "points-based grading, and each category's weight and average.",
                 annotations=_reads("Get grades"))
    async def get_grades(include_past_terms: bool = Field(default=False,
                                                          description="Include terms that have ended.")
                         ) -> dict[str, Any]:
        async with helium() as (client, time_zone):
            grades = await client.get_grades()
            groups = await client.list_course_groups()
        today = _today(time_zone)
        term_ids = {group["id"] for group in groups
                    if include_past_terms or date.fromisoformat(group["end_date"]) >= today}
        return {"terms": shape_grades(grades, term_ids)}

    return server


class HeliumServer(MCPServer[Any]):
    """
    The MCP server, reporting arguments that don't match a tool's input schema as plain problems (by default they
    carry pydantic's error codes and links).
    """

    async def call_tool(self,
                        name: str,
                        arguments: dict[str, Any],
                        context: Context[Any, Any] | None = None) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as e:
            if isinstance(e, UnexpectedToolError) or not isinstance(e.__cause__, ValidationError):
                raise
            raise ToolError(_explain_input(e.__cause__)) from e.__cause__


def _explain_input(error: ValidationError) -> str:
    problems = [f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}" for detail in error.errors()]
    return "The input doesn't match the tool's schema:\n" + "\n".join(f"- {problem}" for problem in problems)


def _reads(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                           open_world_hint=False)


async def _existing_term(client: HeliumClient, plan: TermPlan) -> bool:
    try:
        groups = await client.list_course_groups()
    except HeliumPermissionError:
        return False
    return any(group["title"] == plan.course_group.title
               and group["start_date"] == plan.course_group.start_date.isoformat()
               and group["end_date"] == plan.course_group.end_date.isoformat() for group in groups)


def _tool_error(e: HeliumError) -> ToolError:
    if isinstance(e, HeliumAuthError):
        return ToolError("Helium signed this connection out. Reconnect Helium and sign in, then try again.")
    elif isinstance(e, HeliumPermissionError):
        return ToolError("This Helium connection wasn't given access for that. Reconnect Helium and allow both "
                         "reading the planner and importing, then try again.")
    elif isinstance(e, HeliumRateLimitedError):
        return ToolError(f"Helium asked to slow down. Try again in {(e.meta or {}).get('retry_after')} seconds.")
    elif isinstance(e, HeliumConnectionError):
        logger.warning(f"Helium unreachable: {e}")
        return ToolError("Helium couldn't be reached. Try again in a minute.")
    return ToolError(f"Helium couldn't do that: {e}")


def _time_zone(user: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo((user.get("settings") or {}).get("time_zone") or "UTC")


def _today(time_zone: ZoneInfo) -> date:
    return datetime.now(time_zone).date()


def _read_window(time_zone: ZoneInfo,
                 start: date | None,
                 end: date | None,
                 *,
                 backward: bool = False,
                 searching: bool = False,
                 course: dict[str, Any] | None = None,
                 course_groups: list[dict[str, Any]] | None = None) -> tuple[date, date]:
    if searching and not backward and start is None and end is None:
        dates = _course_dates(course) if course else _current_terms(course_groups or [], _today(time_zone))
        if dates:
            return dates
    return _window(start, end, time_zone, backward=backward)


def _course_dates(course: dict[str, Any]) -> tuple[date, date] | None:
    if not course.get("start_date") or not course.get("end_date"):
        return None
    start = date.fromisoformat(course["start_date"])
    return start, min(date.fromisoformat(course["end_date"]), start + timedelta(days=MAX_WINDOW_DAYS))


def _paged(name: str, items: list[dict[str, Any]], limit: int, page: int) -> dict[str, Any]:
    offset = (page - 1) * limit
    return {"total": len(items), "page": page, "has_more": offset + limit < len(items),
            name: items[offset:offset + limit]}


def _current_terms(course_groups: list[dict[str, Any]], today: date) -> tuple[date, date] | None:
    current = [group for group in course_groups if date.fromisoformat(group["end_date"]) >= today]
    if not current:
        return None
    start = min(date.fromisoformat(group["start_date"]) for group in current)
    end = max(date.fromisoformat(group["end_date"]) for group in current)
    return start, min(end, start + timedelta(days=MAX_WINDOW_DAYS))


def _window(start: date | None, end: date | None, time_zone: ZoneInfo, backward: bool = False) -> tuple[date, date]:
    if backward and start is None and end is None:
        end = _today(time_zone)
        start = end - timedelta(days=MAX_WINDOW_DAYS - 1)
    start = start or _today(time_zone)
    end = end or start + timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    if end < start:
        raise ToolError(f"The window's end ({end}) is before its start ({start}).")
    days = (end - start).days + 1
    if days > MAX_WINDOW_DAYS:
        raise ToolError(f"The window is {days} days; the most is {MAX_WINDOW_DAYS}.")
    return start, end
