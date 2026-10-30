import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, Tool, ToolAnnotations
from pydantic import Field, ValidationError

from helium_connector import __version__
from helium_connector.auth import AGENT, HeliumTokenVerifier
from helium_connector.models import ExistingTerm, ImportPlan, TermPlan
from helium_connector.planning import build_import, describe_import, existing_term, explain_platform_errors, \
    summarize, to_confirm, validate, warnings
from helium_connector.reads import due_between, expand_events, shape_assignments, shape_classes, shape_grades, \
    shape_needed_grade, shape_notes, shape_resources
from helium_connector.settings import Settings
from heliumedu.client import HeliumClient
from heliumedu.exception import (
    HeliumAuthError,
    HeliumConnectionError,
    HeliumError,
    HeliumNotFoundError,
    HeliumPermissionError,
    HeliumRateLimitedError,
    HeliumValidationError,
)

logger = logging.getLogger(__name__)

#: Where the connector's documentation is.
DOCS_URL: Final = ("https://www.heliumedu.com/support/getting-started/where-to-start-with-helium/"
                   "#use-ai-as-a-scheduling-assistant")
#: How many days a read covers when no window is given.
DEFAULT_WINDOW_DAYS: Final = 7
#: The longest window a read covers, in days.
MAX_WINDOW_DAYS: Final = 366
#: How many items a list returns at a time unless asked for more.
DEFAULT_PAGE_SIZE: Final = 25
#: The most items a list returns at a time.
MAX_PAGE_SIZE: Final = 100
#: How the import tools describe allowing duplicates.
DUPLICATES_DESCRIPTION: Final = ("Import even if the new term (same title and dates) or a class (same title, in an "
                                 "existing term) is already in Helium.")
#: How the .ics tools describe their file.
ICS_DESCRIPTION: Final = "The calendar, as the .ics file's text, unchanged."
#: How the .ics tools describe where its entries go.
ICS_CLASS_DESCRIPTION: Final = ("The class whose assignments the entries become (an id from list_classes, or from "
                                "what import_term returned). Unset imports them as events.")
#: The import sections an .ics import fills, by the name the student knows them by.
_ICS_SECTION_NAMES: Final = {"homework": "assignments", "events": "events"}
#: Closes every passing preview: not every client applies INSTRUCTIONS, but each reads the tool's result.
BEFORE_IMPORT: Final = ("Nothing is saved yet. Show the student this summary, including anything to confirm, and "
                        "import only after they confirm.")
#: What the server tells the model it's for.
INSTRUCTIONS: Final = (
    "Helium is a free student planner. These tools set up a term in the student's Helium account (classes, meeting "
    "times, grading categories, assignments, and events), and read the student's assignments, events, notes, "
    "classes, resources, and grades.\n\n"
    "A term comes from the student's syllabi, plus any .ics calendar files they have (a professor's assignment "
    "schedule, or one exported from another planner) and what they tell you. Read the syllabi into a plan for "
    "import_term. Don't copy a calendar file's entries into the plan: once its class is in Helium, import the file's "
    "text with import_ics, which adds its entries as that class's assignments (or as events, with no class). Ask the "
    "student which class a calendar is for when it isn't clear. A calendar has only titles, dates, and times; "
    "grading, meeting times, and credits still come from the syllabus or the student. A Helium export file (a "
    "backup, or a schedule shared by another student) imports in the Helium app under Settings, Import / Export.\n\n"
    "A term holds every class the student takes in it. Imports only add to Helium, and nothing they add can be "
    "changed or deleted through these tools, so gather everything before importing: the syllabus for each class, any "
    "calendar files, and the student's answers. Then ask whether they have more syllabi, calendars, or details for "
    "the term, and import only once they say they're ready. Something that comes later still has a place: a class "
    "goes into its term with import_term, the plan's term given by id (list_classes gives it), and a calendar into "
    "its class with import_ics.\n\n"
    "Syllabi often leave out what the student knows: the term's dates and days off, credits, the room or whether a "
    "class is online, meeting times that differ by day, whether a lecture and its lab are graded together, the "
    "grading scheme, due dates for recurring work, and due times for work due in class. Ask the student about "
    "anything the sources leave unclear rather than guessing, and leave a value unset when it's still unknown.\n\n"
    "preview_import_term and preview_import_ics check an import with Helium without saving anything. "
    "preview_import_term returns problems to fix, warnings, what the plan leaves unset (to_confirm), and a summary to "
    "confirm with the student before importing. Dates and times in a plan are local: Helium places them in "
    "the time zone set in the student's Helium account, which the previews return.\n\n"
    "When the student asks what to work on or study, read before suggesting: overdue and incomplete assignments, "
    "what's due soon, today's meeting times and events (for the time they have free), and grades with their trends. "
    "Suggest a few things that fit their time: overdue work and what's due soonest first, then heavily weighted work "
    "and classes whose grade is down, counting the priority they've set. Let them choose. For the score needed on "
    "remaining work, use get_grades with target_grade rather than calculating it. Help them plan and study, but "
    "don't do graded work for them."
)
#: The guided syllabus import, for clients that offer prompts.
SYLLABUS_PROMPT: Final = """Set up my term in Helium from my syllabi.

1. Ask me to attach the syllabus for every class I'm taking this term, and any .ics calendar files I have (like a \
professor's assignment schedule). Keep asking until I say that's everything. If the term is already in Helium \
(list_classes shows it), import new classes into it by its id. If I have a Helium export file, tell me to import it \
in the Helium app under Settings, Import / Export.
2. Read every syllabus, then ask me about anything they don't make clear. Use only my files and my answers; don't \
fill gaps from what's typical. Ask especially about:
   - the term's first and last days, and days with no class (holidays, breaks)
   - credits for each class
   - whether a lecture and its lab, recitation, or discussion are graded together or separately
   - whether each class is online or in person, and its room
   - meeting times that differ by day, or a rotating schedule (A/B weeks, A/B days)
   - the actual due dates of recurring work, like weekly problem sets
   - the time of day for work due in class or due "that week"
   - how each class is graded: weighted categories and their weights, or points
   - which class each calendar file is for
   If something is still unknown after asking, leave it out rather than guessing.
3. Check the plan with preview_import_term. Fix every problem it lists, and ask me about what it lists under \
to_confirm and warnings.
4. Imports can't be undone here, so before importing, show me the summary and ask whether I have anything else to add \
for this term. Import only after I say I'm ready.
5. After the classes are in Helium, import each calendar file into its class with import_ics (check it first with \
preview_import_ics), using the class ids the import returned."""
#: The guided study session, for clients that offer prompts.
STUDY_PROMPT: Final = """Help me decide what to work on with the time I have.

1. Ask how much time I have, if I haven't said.
2. Read my planner: overdue and incomplete assignments, what's due in the next few days, today's classes and events, \
and my grades and their trends.
3. Suggest up to three things that fit my time: overdue work and what's due soonest first, then heavily weighted work \
and classes whose grade is down. Count the priority I've set on assignments, and say why each one made the list.
4. For an exam coming up, suggest spreading review over the days before it, and quizzing myself from that class's \
notes (list_notes with its class_id).
5. If I ask what I need on remaining work, use get_grades with target_grade; don't calculate it.
6. Let me choose. Help me plan and study, but don't do graded work for me."""

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
                time_zone = ZoneInfo((user.get("settings") or {}).get("time_zone") or "UTC")
                yield HeliumClient(session, token.token, settings.api_url, agent=AGENT), time_zone
        except HeliumError as e:
            raise _tool_error(e) from e

    async def needed_grade(target_grade: float, class_id: int | None, target_id: int | None) -> dict[str, Any]:
        if class_id is None or target_id is None:
            return {"problems": ["target_grade needs class_id and target_id."]}
        async with helium() as (client, _):
            try:
                needed = await client.get_needed_grade(class_id, target_id, target_grade)
            except HeliumValidationError as e:
                errors = (e.meta or {}).get("errors")
                messages = errors.values() if isinstance(errors, dict) else [errors]
                return {"problems": [str(message) for value in messages
                                     for message in (value if isinstance(value, list) else [value])]}
            except HeliumNotFoundError:
                return {"problems": [f"Class {class_id} isn't in Helium. get_grades gives each class's id."]}
        return {"class_id": class_id, "target_grade": target_grade, **shape_needed_grade(needed)}

    async def import_plan(plan: ImportPlan, *, dry_run: bool, allow_duplicates: bool) -> dict[str, Any]:
        outcome = "ready" if dry_run else "imported"
        async with helium() as (client, time_zone):
            course_group_id = None
            if isinstance(plan.course_group, ExistingTerm):
                course_group_id = plan.course_group.id
                course_group = next((group for group in await client.list_course_groups()
                                     if group["id"] == course_group_id), None)
                if course_group is None:
                    return {outcome: False, "problems": [f"Term {course_group_id} isn't in Helium. list_classes "
                                                         f"gives each term's id."]}
                term = existing_term(course_group)
            else:
                term = plan.course_group
            term_plan = TermPlan(term=term, classes=plan.courses, events=plan.events)
            problems = validate(term_plan)
            if not problems and not allow_duplicates:
                problems = await _duplicates(client, term_plan, course_group_id)
            if problems:
                return {outcome: False, "problems": problems, **_notes(term_plan)}
            return await send(client, time_zone, term_plan, dry_run=dry_run, course_group_id=course_group_id)

    async def import_ics_file(ics: str, course_id: int | None, *, dry_run: bool) -> dict[str, Any]:
        outcome = "ready" if dry_run else "imported"
        async with helium() as (client, time_zone):
            try:
                result = await client.import_ics(ics, course_id=course_id, dry_run=dry_run)
            except HeliumValidationError as e:
                return {outcome: False, "problems": _explain_ics_errors((e.meta or {}).get("errors"), course_id)}
        created = {name: result.counts[section] for section, name in _ICS_SECTION_NAMES.items()
                   if result.counts.get(section)}
        if dry_run:
            return {"ready": True, "saved": False, "before_import": BEFORE_IMPORT, "time_zone": time_zone.key,
                    "class_id": course_id, "would_create": created}
        logger.info(f"Imported an .ics file into {'a class' if course_id else 'events'}")
        return {"imported": True, "class_id": course_id, "created": created,
                "open_in_helium": f"{settings.app_url}/planner"}

    async def send(client: HeliumClient,
                   time_zone: ZoneInfo,
                   plan: TermPlan,
                   *,
                   dry_run: bool,
                   course_group_id: int | None = None) -> dict[str, Any]:
        outcome = "ready" if dry_run else "imported"
        payload = build_import(plan, time_zone)
        try:
            result = await client.import_data(payload.data, dry_run=dry_run, course_group_id=course_group_id)
        except HeliumValidationError as e:
            return {outcome: False, **_notes(plan),
                    "problems": explain_platform_errors((e.meta or {}).get("errors"), payload.labels)}
        if dry_run:
            return {"ready": True, "saved": False, "before_import": BEFORE_IMPORT, "time_zone": time_zone.key,
                    "summary": summarize(plan), **_notes(plan)}
        logger.info(f"Imported {len(plan.courses)} class(es)")
        return {"imported": True, **describe_import(plan, result.counts, result.ids, course_group_id),
                **_notes(plan), "summary": summarize(plan), "open_in_helium": f"{settings.app_url}/classes"}

    @server.tool(title="Preview a term import",
                 description="Check a term's assignments, events, classes, meeting times, and grading categories "
                             "against what Helium accepts, without saving anything: a new term, or classes for one "
                             "already in Helium. Returns every problem to fix, or a summary of what import_term would "
                             "create, with what the plan leaves unset.",
                 annotations=_reads("Preview a term import"))
    async def preview_import_term(plan: ImportPlan,
                                  allow_duplicates: bool = Field(
                                      default=False, description=DUPLICATES_DESCRIPTION)) -> dict[str, Any]:
        return await import_plan(plan, dry_run=True, allow_duplicates=allow_duplicates)

    @server.tool(title="Import a term",
                 description="Save a term's assignments, events, classes, meeting times, and grading categories to "
                             "Helium, all at once: as a new term, or into one already in Helium. Adds to the account; "
                             "changes nothing already there. preview_import_term shows what it would create.",
                 annotations=ToolAnnotations(title="Import a term", read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=False))
    async def import_term(plan: ImportPlan,
                          allow_duplicates: bool = Field(default=False,
                                                         description=DUPLICATES_DESCRIPTION)) -> dict[str, Any]:
        return await import_plan(plan, dry_run=False, allow_duplicates=allow_duplicates)

    @server.tool(title="Preview an .ics import",
                 description="Check an .ics calendar file against what Helium accepts, without saving anything: "
                             "how many assignments (in a class already in Helium) or events its entries would "
                             "become. Returns the problem if Helium can't import it.",
                 annotations=_reads("Preview an .ics import"))
    async def preview_import_ics(ics: str = Field(description=ICS_DESCRIPTION),
                                 class_id: int | None = Field(default=None,
                                                              description=ICS_CLASS_DESCRIPTION)) -> dict[str, Any]:
        return await import_ics_file(ics, class_id, dry_run=True)

    @server.tool(title="Import an .ics file",
                 description="Save an .ics calendar file's entries to Helium, all at once: as assignments in a class "
                             "already in Helium (uncategorized and ungraded, since a calendar has only titles, "
                             "dates, and times), or as events. Repeating entries are expanded, and cancelled ones "
                             "left out. Adds to the account; changes nothing already there. Importing the same file "
                             "twice adds its entries twice. preview_import_ics shows what it would create.",
                 annotations=ToolAnnotations(title="Import an .ics file", read_only_hint=False,
                                             destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    async def import_ics(ics: str = Field(description=ICS_DESCRIPTION),
                         class_id: int | None = Field(default=None,
                                                      description=ICS_CLASS_DESCRIPTION)) -> dict[str, Any]:
        return await import_ics_file(ics, class_id, dry_run=False)

    @server.prompt(title="Import syllabi",
                   description="Set up a term in Helium from your syllabi, answering questions about anything they "
                               "leave unclear.")
    def import_syllabi() -> str:
        return SYLLABUS_PROMPT

    @server.prompt(title="Plan study time",
                   description="Decide what to work on or study with the time you have, from your planner and "
                               "grades.")
    def plan_study_time() -> str:
        return STUDY_PROMPT

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
                               category_id: int | None = Field(default=None,
                                                               description="Only this category (an id from "
                                                                           "list_classes)."),
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
                                                  overdue=True if status == "overdue" else None,
                                                  category_ids=[category_id] if category_id is not None else None,
                                                  search=query)
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
                 description="List the user's notes as plain text: all of them, those matching a query, a "
                             "class's, or the note on one assignment, event, or resource. Returns limit at a time, "
                             "with total and has_more; pass page for the next.",
                 annotations=_reads("List notes"))
    async def list_notes(query: str | None = Field(default=None,
                                                   description="Only notes whose title, text, or linked item "
                                                               "matches this text."),
                         class_id: int | None = Field(default=None,
                                                      description="Only notes on this class's assignments or "
                                                                  "resources (an id from list_classes)."),
                         assignment_id: int | None = Field(default=None,
                                                           description="Only the note on this assignment (an id "
                                                                       "from list_assignments)."),
                         event_id: int | None = Field(default=None,
                                                      description="Only the note on this event (an id from "
                                                                  "list_events)."),
                         resource_id: int | None = Field(default=None,
                                                         description="Only the note on this resource (an id from "
                                                                     "list_resources)."),
                         limit: int = Field(default=5, ge=1, le=10),
                         page: int = Field(default=1, ge=1)) -> dict[str, Any]:
        async with helium() as (client, time_zone):
            notes = await client.list_notes_page(query, homework_id=assignment_id, course_id=class_id,
                                                 event_id=event_id, resource_id=resource_id, page=page,
                                                 page_size=limit)
            courses = await client.list_courses()
        return {"total": notes.total, "page": page, "has_more": notes.has_more,
                "notes": shape_notes(notes.items, courses, time_zone, limit)}

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
                 description="Get the user's grades: each term's classes, with overall grade, trend (up, down, "
                             "or steady), weighted or points-based grading, and each category's weight, average, "
                             "and ungraded assignments. With target_grade, class_id, and target_id, returns instead "
                             "the score Helium calculates the class needs to reach target_grade.",
                 annotations=_reads("Get grades"))
    async def get_grades(include_past_terms: bool = Field(default=False,
                                                          description="Include terms that have ended."),
                         target_grade: float | None = Field(default=None,
                                                            description="The overall class grade wanted (0-100)."),
                         class_id: int | None = Field(default=None,
                                                      description="With target_grade: the class (an id from "
                                                                  "get_grades)."),
                         target_id: int | None = Field(default=None,
                                                       description="With target_grade: in a weighted class, a "
                                                                   "category down to its last ungraded "
                                                                   "assignment; otherwise, an ungraded assignment "
                                                                   "(an id from list_assignments).")
                         ) -> dict[str, Any]:
        if target_grade is not None:
            return await needed_grade(target_grade, class_id, target_id)
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

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema = _inline_definitions(tool.input_schema)
            if tool.output_schema:
                tool.output_schema = _inline_definitions(tool.output_schema)
            _drop_field_titles(tool.input_schema)
        return tools

    async def call_tool(self,
                        name: str,
                        arguments: dict[str, Any],
                        context: Context[Any, Any] | None = None) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as e:
            if isinstance(e, UnexpectedToolError) or not isinstance(e.__cause__, ValidationError):
                raise
            cause = e.__cause__
            problems = [f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
                        for detail in cause.errors()]
            raise ToolError("The input doesn't match the tool's schema:\n"
                            + "\n".join(f"- {problem}" for problem in problems)) from cause


def _inline_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Self-contain a tool schema: each ``$ref`` becomes the definition it names, and ``$defs`` goes. ChatGPT validates
    arguments against ``$defs`` but doesn't show them to its model, which then guesses nested shapes such as a class's
    schedules. Pydantic's ``discriminator`` goes too: it isn't JSON Schema, and each ``oneOf`` branch fixes its own
    ``pattern``.

    :param schema: A pydantic-generated JSON schema.
    :return: The same schema with no ``$ref``, ``$defs``, or ``discriminator``.
    """
    return cast(dict[str, Any], _resolve_references(schema, schema.get("$defs", {})))


def _resolve_references(node: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve_references(item, definitions) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        return _resolve_references({**definitions[node["$ref"].removeprefix("#/$defs/")], **siblings}, definitions)
    return {key: ({name: _resolve_references(field, definitions) for name, field in value.items()}
                  if key == "properties" else _resolve_references(value, definitions))
            for key, value in node.items() if key not in ("$defs", "discriminator")}


def _drop_field_titles(schema: Any) -> None:
    """
    Drop the titles pydantic generates from field names ("Start Time" for start_time): they restate the name and
    cost tokens in every tool listing. Model titles (Term, Class, Assignment) stay.
    """
    if isinstance(schema, dict):
        for name, field in (schema.get("properties") or {}).items():
            if isinstance(field, dict) and str(field.get("title", "")).replace(" ", "_").casefold() == name.casefold():
                del field["title"]
        for value in schema.values():
            _drop_field_titles(value)
    elif isinstance(schema, list):
        for value in schema:
            _drop_field_titles(value)


def _reads(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                           open_world_hint=False)


async def _duplicates(client: HeliumClient, plan: TermPlan, course_group_id: int | None) -> list[str]:
    """
    What's already in Helium: the new term (same title and dates), or classes already in an existing term. Skipped when
    the connection can't read what's there.
    """
    try:
        if course_group_id is None:
            groups = await client.list_course_groups()
            term = plan.course_group
            if any(group["title"] == term.title and group["start_date"] == term.start_date.isoformat()
                   and group["end_date"] == term.end_date.isoformat() for group in groups):
                return [f"A term named '{term.title}' with these dates is already in Helium. Set allow_duplicates "
                        f"to import a second copy, or import into it with the term's id."]
            return []
        courses = await client.list_courses()
    except HeliumPermissionError:
        return []
    titles = {course["title"].casefold() for course in courses if course.get("course_group") == course_group_id}
    return [f"Class '{course.title}' is already in term '{plan.course_group.title}'. Set allow_duplicates to add a "
            f"second copy." for course in plan.courses if course.title.casefold() in titles]


def _explain_ics_errors(errors: Any, course_id: int | None) -> list[str]:
    if isinstance(errors, dict) and "course" in errors:
        return [f"Class {course_id} isn't in Helium. list_classes gives each class's id."]
    if isinstance(errors, dict) and "details" in errors:
        details = errors["details"]
        return [str(detail) for detail in details] if isinstance(details, list) else [str(details)]
    return explain_platform_errors(errors, {})


def _notes(plan: TermPlan) -> dict[str, list[str]]:
    return {"warnings": warnings(plan), "to_confirm": to_confirm(plan)}


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
