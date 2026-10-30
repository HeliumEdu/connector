import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import jsonschema
from starlette.applications import Starlette

from helium_connector import app as app_module
from helium_connector.app import create_app
from helium_connector.models import Course
from helium_connector.server import DOCS_URL
from helium_connector.settings import Settings
from tests.testcase import IMPORT_COUNTS, TOKEN, AsyncTestCase, given_a_plan, uploaded_import

CONNECTOR_URL = "https://mcp.example.test"
PROTOCOL_VERSION = "2025-11-25"
#: The revision whose `subscriptions/listen` replaced the GET stream.
SUBSCRIPTIONS_PROTOCOL_VERSION = "2026-07-28"


class McpTestCase(AsyncTestCase):
    async def asyncSetUp(self) -> None:
        self.app = create_app(Settings(connector_url=CONNECTOR_URL), self.helium.transport())
        self.stopping = asyncio.Event()
        started = asyncio.Event()

        async def run_lifespan() -> None:
            async with self.app.router.lifespan_context(self.app):
                started.set()
                await self.stopping.wait()

        self.lifespan = asyncio.create_task(run_lifespan())
        await started.wait()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app, raise_app_exceptions=False),
                                        base_url=CONNECTOR_URL)
        self.next_id = 0

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.stopping.set()
        await self.lifespan

    async def rpc(self,
                  method: str,
                  params: dict[str, Any] | None = None,
                  token: str | None = TOKEN) -> httpx.Response:
        self.next_id += 1
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            body["params"] = params
        return await self.client.post("/mcp", headers=headers, json=body)

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        response = await self.rpc("tools/call", {"name": tool, "arguments": arguments})
        self.assertEqual(200, response.status_code, response.text)
        result: dict[str, Any] = response.json()["result"]
        return result

    def plan_arguments(self, **changes: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = json.loads(given_a_plan(**changes).model_dump_json())
        return arguments


class TestAuthorization(McpTestCase):
    async def test_unauthenticated_request_is_challenged(self) -> None:
        # WHEN
        response = await self.rpc("tools/list", token=None)

        # THEN
        self.assertEqual(401, response.status_code)
        self.assertIn(f'resource_metadata="{CONNECTOR_URL}/.well-known/oauth-protected-resource/mcp"',
                      response.headers["WWW-Authenticate"])
        self.assertIn('scope="planner:read planner:import"', response.headers["WWW-Authenticate"])
        self.assertEqual([], self.helium.requests)

    async def test_token_helium_refuses_is_challenged(self) -> None:
        # WHEN
        response = await self.rpc("tools/list", token="revoked")

        # THEN
        self.assertEqual(401, response.status_code)

    async def test_protected_resource_metadata(self) -> None:
        # WHEN
        response = await self.client.get("/.well-known/oauth-protected-resource/mcp")

        # THEN
        self.assertEqual(200, response.status_code)
        self.assertEqual(f"{CONNECTOR_URL}/mcp", response.json()["resource"])
        self.assertEqual(["https://api.heliumedu.com"], response.json()["authorization_servers"])
        self.assertEqual(["planner:read", "planner:import"], response.json()["scopes_supported"])

    async def test_subscriptions_are_not_offered(self) -> None:
        # GIVEN
        meta = {"io.modelcontextprotocol/protocolVersion": SUBSCRIPTIONS_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {}}

        def request(method: str, params: dict[str, Any]) -> Any:
            return self.client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                                     "MCP-Protocol-Version": SUBSCRIPTIONS_PROTOCOL_VERSION,
                                                     "Mcp-Method": method, "Authorization": f"Bearer {TOKEN}"},
                                    json={"jsonrpc": "2.0", "id": 1, "method": method,
                                          "params": {"_meta": meta, **params}})

        # WHEN
        discovered = await request("server/discover", {})
        listen = await asyncio.wait_for(request("subscriptions/listen", {"notifications": {"toolsListChanged": True}}),
                                        timeout=5)

        # THEN
        self.assertFalse(discovered.json()["result"]["capabilities"]["tools"]["listChanged"])
        self.assertEqual(-32601, listen.json()["error"]["code"])

    async def test_event_stream_is_not_offered(self) -> None:
        # WHEN
        response = await self.client.get("/mcp", headers={"Accept": "text/event-stream",
                                                          "Authorization": f"Bearer {TOKEN}"})

        # THEN
        self.assertEqual(405, response.status_code)
        self.assertEqual("POST", response.headers["Allow"])

    async def test_browser_is_sent_to_the_docs(self) -> None:
        # GIVEN
        browser = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

        # WHEN
        responses = [await self.client.get("/mcp", headers=browser), await self.client.get("/", headers=browser)]

        # THEN
        self.assertEqual([302, 302], [response.status_code for response in responses])
        self.assertEqual({DOCS_URL}, {response.headers["Location"] for response in responses})

    async def test_helium_unreachable_is_not_a_challenge(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/auth/user/", 503, None)

        # WHEN
        response = await self.rpc("tools/list")

        # THEN
        self.assertEqual(500, response.status_code)

    async def test_every_response_carries_the_header(self) -> None:
        # WHEN
        responses = [await self.client.get("/status"), await self.rpc("tools/list", token=None)]

        # THEN
        self.assertEqual(["42", "42"], [response.headers["X-Muppets"] for response in responses])


class TestTools(McpTestCase):
    async def test_tool_list(self) -> None:
        # WHEN
        response = await self.rpc("tools/list")

        # THEN
        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        self.assertEqual({"preview_import_term", "import_term", "preview_import_ics", "import_ics",
                          "list_assignments", "list_events", "list_notes", "list_classes", "list_resources",
                          "get_grades"}, set(tools))
        writes = [name for name, tool in tools.items() if not tool["annotations"]["readOnlyHint"]]
        self.assertEqual(["import_term", "import_ics"], writes)
        for name in ("preview_import_term", "import_term"):
            self.assertEqual("object", tools[name]["inputSchema"]["properties"]["plan"]["type"], name)
        for name, tool in tools.items():
            self.assertTrue(tool["title"], name)
            self.assertEqual(tool["title"], tool["annotations"]["title"], name)
            self.assertFalse(tool["annotations"]["destructiveHint"], name)
            self.assertFalse(tool["annotations"]["openWorldHint"], name)
            self.assertLessEqual(len(name), 64)

    async def test_field_titles_are_left_out_and_model_titles_kept(self) -> None:
        # WHEN
        response = await self.rpc("tools/list")

        # THEN
        tool = next(tool for tool in response.json()["result"]["tools"] if tool["name"] == "import_term")
        schema = tool["inputSchema"]
        plan = schema["properties"]["plan"]
        course = plan["properties"]["classes"]["items"]
        weekly = course["properties"]["schedules"]["items"]["oneOf"][0]
        self.assertNotIn("title", schema["properties"]["allow_duplicates"])
        self.assertNotIn("title", weekly["properties"]["meetings"]["items"]["properties"]["start_time"])
        self.assertEqual(["Term", "Existing term"], [term["title"] for term in plan["properties"]["term"]["anyOf"]])
        self.assertEqual(("Class", "Assignment"),
                         (course["title"], course["properties"]["assignments"]["items"]["title"]))

    async def test_schemas_are_self_contained(self) -> None:
        # WHEN
        response = await self.rpc("tools/list")

        # THEN
        for tool in response.json()["result"]["tools"]:
            schemas = json.dumps([tool["inputSchema"], tool.get("outputSchema")])
            for keyword in ('"$ref"', '"$defs"', '"discriminator"'):
                self.assertNotIn(keyword, schemas, f"{tool['name']} lists {keyword}, which ChatGPT's model can't see")

    async def test_import_schema_accepts_a_class_schedule_and_rejects_a_guessed_one(self) -> None:
        # GIVEN
        response = await self.rpc("tools/list")
        tool = next(tool for tool in response.json()["result"]["tools"] if tool["name"] == "preview_import_term")
        plan = self.plan_arguments()
        guessed = json.loads(json.dumps(plan))
        guessed["classes"][0]["schedules"] = [{"pattern": "weekly", "days": ["monday", "wednesday", "friday"],
                                               "start_time": "10:00", "end_time": "10:50"}]

        # WHEN/THEN
        jsonschema.validate({"plan": plan}, tool["inputSchema"])
        with self.assertRaises(jsonschema.ValidationError, msg="ChatGPT's first guess at a weekly schedule"):
            jsonschema.validate({"plan": guessed}, tool["inputSchema"])


class TestPreview(McpTestCase):
    async def test_ready_plan_is_dry_run(self) -> None:
        # WHEN
        result = await self.call("preview_import_term", plan=self.plan_arguments())

        # THEN
        content = result["structuredContent"]
        self.assertTrue(content["ready"])
        self.assertEqual("America/Chicago", content["time_zone"])
        self.assertEqual(3, content["summary"]["totals"]["assignments"])
        request = self.helium.requested("POST", "/importexport/import/")[0]
        self.assertEqual("true", request.url.params["dry_run"])
        self.assertEqual("2026-09-14T23:59:00-05:00", uploaded_import(request)["homework"][0]["start"])

    async def test_problems_stop_before_helium(self) -> None:
        # WHEN
        result = await self.call("preview_import_term",
                                 plan=self.plan_arguments(courses=[Course(title="ART", end_date=date(2027, 1, 1))]))

        # THEN
        self.assertFalse(result["structuredContent"]["ready"])
        self.assertEqual(1, len(result["structuredContent"]["problems"]))
        self.assertIn("Class 'ART': no meeting times, so it won't show on the calendar.",
                      result["structuredContent"]["to_confirm"])
        self.assertEqual([], self.helium.requested("POST", "/importexport/import/"))

    async def test_helium_rejection_names_the_item(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/", 400,
                           {"homework": ["homework id 1, title: Ensure this field has no more than 255 characters."]})

        # WHEN
        result = await self.call("preview_import_term", plan=self.plan_arguments())

        # THEN
        self.assertEqual(["Assignment 'Problem Set 1' (BIO 151): title: Ensure this field has no more than 255 "
                          "characters."], result["structuredContent"]["problems"])

    async def test_existing_term_is_refused(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200,
                           [{"id": 4, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18"}])

        # WHEN
        result = await self.call("preview_import_term", plan=self.plan_arguments())

        # THEN
        self.assertFalse(result["structuredContent"]["ready"])
        self.assertIn("already in Helium", result["structuredContent"]["problems"][0])
        self.assertEqual([], self.helium.requested("POST", "/importexport/import/"))

    async def test_schema_rejects_unknown_fields(self) -> None:
        # GIVEN
        plan = self.plan_arguments()
        plan["classes"][0]["grade_scale"] = "A-F"

        # WHEN
        result = await self.call("preview_import_term", plan=plan)

        # THEN
        self.assertTrue(result["isError"])
        self.assertEqual([], self.helium.requested("POST", "/importexport/import/"))

    async def test_schema_errors_are_plain_problems(self) -> None:
        # GIVEN
        plan = self.plan_arguments()
        plan["classes"][0]["categories"][0]["weight"] = "abc"

        # WHEN
        result = await self.call("preview_import_term", plan=plan)

        # THEN
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("- plan.classes.0.categories.0.weight: Input should be a valid decimal", text)
        self.assertNotIn("pydantic.dev", text)
        self.assertNotIn("abc", text)


class TestImport(McpTestCase):
    async def test_import(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/", 200,
                           {**IMPORT_COUNTS, "ids": {"course_groups": {"1": 41}, "courses": {"1": 51, "2": 52}}})

        # WHEN
        result = await self.call("import_term", plan=self.plan_arguments())

        # THEN
        content = result["structuredContent"]
        self.assertTrue(content["imported"])
        self.assertEqual({"terms": 1, "classes": 2, "schedules": 3, "categories": 3, "assignments": 3, "events": 2},
                         content["created"])
        self.assertEqual(41, content["term_id"])
        self.assertEqual([{"title": "BIO 151", "id": 51}, {"title": "ENG 101", "id": 52}], content["classes"])
        self.assertIn("Class 'ENG 101': credits aren't set, so it imports with 0 credits.", content["to_confirm"])
        self.assertEqual("https://app.heliumedu.com/classes", content["open_in_helium"])
        request = self.helium.requested("POST", "/importexport/import/")[0]
        self.assertNotIn("dry_run", request.url.params)

    async def test_duplicate_term_is_refused(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200,
                           [{"id": 4, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18"}])

        # WHEN
        result = await self.call("import_term", plan=self.plan_arguments())

        # THEN
        self.assertFalse(result["structuredContent"]["imported"])
        self.assertEqual([], self.helium.requested("POST", "/importexport/import/"))

    async def test_duplicate_term_when_allowed(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200,
                           [{"id": 4, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18"}])

        # WHEN
        result = await self.call("import_term", plan=self.plan_arguments(), allow_duplicates=True)

        # THEN
        self.assertTrue(result["structuredContent"]["imported"])

    async def test_import_only_token_skips_the_duplicate_check(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 403, {"detail": "insufficient scope"})

        # WHEN
        result = await self.call("import_term", plan=self.plan_arguments())

        # THEN
        self.assertTrue(result["structuredContent"]["imported"])


class TestImportIntoAnExistingTerm(McpTestCase):
    TERM = {"id": 4, "title": "Fall 2026", "start_date": "2026-08-31", "end_date": "2026-12-18",
            "exceptions": "20260907"}

    def classes_arguments(self, term_id: int = 4) -> dict[str, Any]:
        plan = self.plan_arguments()
        return {"term": {"id": term_id}, "classes": plan["classes"], "events": plan["events"]}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.helium.answer("GET", "/planner/coursegroups/", 200, [self.TERM])
        self.helium.answer("GET", "/planner/courses/", 200, [{"id": 9, "title": "CHEM 142", "course_group": 4}])

    async def test_preview_into_an_existing_term(self) -> None:
        # WHEN
        result = await self.call("preview_import_term", plan=self.classes_arguments())

        # THEN
        content = result["structuredContent"]
        self.assertTrue(content["ready"], content)
        self.assertEqual("Fall 2026", content["summary"]["term"]["title"])
        request = self.helium.requested("POST", "/importexport/import/")[0]
        self.assertEqual(("true", "4"), (request.url.params["dry_run"], request.url.params["course_group"]))

    async def test_import_into_an_existing_term(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/", 200,
                           {**IMPORT_COUNTS, "course_groups": 0, "ids": {"courses": {"1": 51, "2": 52}}})

        # WHEN
        result = await self.call("import_term", plan=self.classes_arguments())

        # THEN
        content = result["structuredContent"]
        self.assertTrue(content["imported"], content)
        self.assertEqual(4, content["term_id"])
        self.assertNotIn("terms", content["created"])
        self.assertEqual([{"title": "BIO 151", "id": 51}, {"title": "ENG 101", "id": 52}], content["classes"])
        request = self.helium.requested("POST", "/importexport/import/")[0]
        self.assertEqual("4", request.url.params["course_group"])
        self.assertNotIn("dry_run", request.url.params)

    async def test_unknown_term_is_a_problem(self) -> None:
        # WHEN
        result = await self.call("import_term", plan=self.classes_arguments(term_id=99))

        # THEN
        self.assertFalse(result["structuredContent"]["imported"])
        self.assertIn("Term 99 isn't in Helium", result["structuredContent"]["problems"][0])
        self.assertEqual([], self.helium.requested("POST", "/importexport/import/"))

    async def test_classes_are_checked_against_the_terms_dates(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200, [{**self.TERM, "end_date": "2026-10-01"}])

        # WHEN
        result = await self.call("preview_import_term", plan=self.classes_arguments())

        # THEN
        self.assertFalse(result["structuredContent"]["ready"])
        self.assertIn("Class 'BIO 151', assignment 'Final Exam': due 2026-12-14, outside the class's dates "
                      "(2026-08-31 to 2026-10-01).", result["structuredContent"]["problems"])
        self.assertEqual([], self.helium.requested("POST", "/importexport/import/"))

    async def test_class_already_in_the_term_is_refused(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/courses/", 200, [{"id": 9, "title": "bio 151", "course_group": 4}])

        # WHEN
        refused = await self.call("import_term", plan=self.classes_arguments())
        allowed = await self.call("import_term", plan=self.classes_arguments(), allow_duplicates=True)

        # THEN
        self.assertEqual(["Class 'BIO 151' is already in term 'Fall 2026'. Set allow_duplicates to add a "
                          "second copy."], refused["structuredContent"]["problems"])
        self.assertTrue(allowed["structuredContent"]["imported"])
        self.assertEqual(1, len(self.helium.requested("POST", "/importexport/import/")))


class TestImportIcs(McpTestCase):
    ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Lab 1\r\n"
           "DTSTART:20270126T170000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    async def test_preview_into_a_class(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/ics/", 200,
                           {**{section: 0 for section in IMPORT_COUNTS}, "homework": 4})

        # WHEN
        result = await self.call("preview_import_ics", ics=self.ICS, class_id=52)

        # THEN
        content = result["structuredContent"]
        self.assertEqual({"ready": True, "time_zone": "America/Chicago", "class_id": 52,
                          "would_create": {"assignments": 4}}, content)
        request = self.helium.requested("POST", "/importexport/import/ics/")[0]
        self.assertEqual("true", request.url.params["dry_run"])
        body = request.read().decode("utf-8")
        self.assertIn('name="target_type"\r\n\r\ncourse', body)
        self.assertIn('name="course"\r\n\r\n52', body)
        self.assertIn("SUMMARY:Lab 1", body)

    async def test_import_as_events(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/ics/", 200,
                           {**{section: 0 for section in IMPORT_COUNTS}, "events": 2})

        # WHEN
        result = await self.call("import_ics", ics=self.ICS)

        # THEN
        content = result["structuredContent"]
        self.assertTrue(content["imported"])
        self.assertEqual({"events": 2}, content["created"])
        self.assertIsNone(content["class_id"])
        request = self.helium.requested("POST", "/importexport/import/ics/")[0]
        self.assertNotIn("dry_run", request.url.params)
        self.assertIn('name="target_type"\r\n\r\nevents', request.read().decode("utf-8"))

    async def test_unknown_class_is_a_problem(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/ics/", 400,
                           {"course": ["Unresolved `course` reference `99`."]})

        # WHEN
        result = await self.call("import_ics", ics=self.ICS, class_id=99)

        # THEN
        self.assertEqual({"imported": False,
                          "problems": ["Class 99 isn't in Helium. list_classes gives each class's id."]},
                         result["structuredContent"])

    async def test_calendar_helium_cannot_read_is_a_problem(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/ics/", 400,
                           {"details": "No events in the calendar file could be imported."})

        # WHEN
        result = await self.call("preview_import_ics", ics=self.ICS)

        # THEN
        self.assertEqual(["No events in the calendar file could be imported."],
                         result["structuredContent"]["problems"])


class TestPrompts(McpTestCase):
    async def test_syllabus_prompt(self) -> None:
        # WHEN
        listed = await self.rpc("prompts/list")
        prompt = await self.rpc("prompts/get", {"name": "import_syllabi"})

        # THEN
        self.assertEqual(["import_syllabi", "plan_study_time"],
                         [prompt["name"] for prompt in listed.json()["result"]["prompts"]])
        text = prompt.json()["result"]["messages"][0]["content"]["text"]
        self.assertIn("every class I'm taking this term", text)
        self.assertIn("to_confirm", text)

    async def test_study_prompt_points_grade_questions_at_get_grades(self) -> None:
        # WHEN
        prompt = await self.rpc("prompts/get", {"name": "plan_study_time"})

        # THEN
        text = prompt.json()["result"]["messages"][0]["content"]["text"]
        self.assertIn("how much time I have", text)
        self.assertIn("get_grades with target_grade", text)
        self.assertIn("don't do graded work for me", text)

    async def test_instructions_point_grade_questions_at_get_grades(self) -> None:
        # WHEN
        response = await self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "test", "version": "1"}})

        # THEN
        instructions = response.json()["result"]["instructions"]
        self.assertIn("use get_grades with target_grade rather than calculating it", instructions)


class TestReads(McpTestCase):
    async def test_assignments_default_to_the_next_week(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [])
        self.helium.answer("GET", "/planner/courses/", 200, [])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        result = await self.call("list_assignments", status="incomplete")

        # THEN
        content = result["structuredContent"]
        self.assertEqual(6, (date.fromisoformat(content["end_date"]) - date.fromisoformat(content["start_date"])).days)
        params = self.helium.requested("GET", "/planner/homework/")[0].url.params
        self.assertEqual("false", params["completed"])
        self.assertEqual(9, (date.fromisoformat(params["to"]) - date.fromisoformat(params["from"])).days)

    async def test_overdue_defaults_to_the_past_year(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [])
        self.helium.answer("GET", "/planner/courses/", 200, [])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        result = await self.call("list_assignments", status="overdue")

        # THEN
        content = result["structuredContent"]
        self.assertEqual(365, (date.fromisoformat(content["end_date"]) -
                               date.fromisoformat(content["start_date"])).days)
        self.assertEqual("true", self.helium.requested("GET", "/planner/homework/")[0].url.params["overdue"])

    async def test_assignments_outside_the_window_are_left_out(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [
            {"id": day, "title": f"Quiz {day}", "course": 10, "all_day": True,
             "start": f"2026-09-{day:02d}T05:00:00Z"} for day in (17, 18, 19)])
        self.helium.answer("GET", "/planner/courses/", 200, [])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        result = await self.call("list_assignments", start_date="2026-09-18", end_date="2026-09-18")

        # THEN
        self.assertEqual(["Quiz 18"], [item["title"] for item in result["structuredContent"]["assignments"]])

    async def test_window_counts_both_ends(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/events/", 200, [])

        # WHEN
        widest = await self.call("list_events", start_date="2026-01-01", end_date="2027-01-01")
        result = await self.call("list_events", start_date="2026-01-01", end_date="2027-01-02")

        # THEN
        self.assertFalse(widest["isError"])
        self.assertTrue(result["isError"])
        self.assertIn("The window is 367 days; the most is 366", result["content"][0]["text"])

    async def test_window_too_long(self) -> None:
        # WHEN
        result = await self.call("list_events", start_date="2026-01-01", end_date="2027-06-01")

        # THEN
        self.assertTrue(result["isError"])
        self.assertIn("the most is 366", result["content"][0]["text"])

    async def test_missing_read_access_says_how_to_fix_it(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/grades/", 403, {"detail": "insufficient scope"})

        # WHEN
        result = await self.call("get_grades")

        # THEN
        self.assertTrue(result["isError"])
        self.assertIn("Reconnect Helium", result["content"][0]["text"])

    async def test_needed_grade(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/grades/needed/", 200, {
            "state": "achievable", "needed_grade": 76.25, "weighted": True, "category": {"id": 3, "title": "Exams"},
            "assignment": {"id": 9, "title": "Final", "points_possible": 100.0, "needed_points": 76.25},
            "total_weight_accounted_for": None})

        # WHEN
        result = await self.call("get_grades", target_grade=85, class_id=10, target_id=3)

        # THEN
        content = result["structuredContent"]
        self.assertEqual({"class_id": 10, "target_grade": 85, "outcome": "achievable", "needed_grade": 76.3,
                          "category": "Exams",
                          "assignment": {"title": "Final", "points_possible": 100.0, "needed_points": 76.3},
                          "total_weight": None}, content)
        params = self.helium.requested("GET", "/planner/grades/needed/")[0].url.params
        self.assertEqual({"course": "10", "target": "3", "desired_grade": "85.0"}, dict(params))
        self.assertEqual([], self.helium.requested("GET", "/planner/grades/"))

    async def test_needed_grade_explains_what_helium_refuses(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/grades/needed/", 400,
                           {"target": ["Choose a category with weight and exactly one assignment left."]})

        # WHEN
        result = await self.call("get_grades", target_grade=85, class_id=10, target_id=3)

        # THEN
        self.assertEqual({"problems": ["Choose a category with weight and exactly one assignment left."]},
                         result["structuredContent"])

    async def test_needed_grade_needs_a_class_and_target(self) -> None:
        # WHEN
        result = await self.call("get_grades", target_grade=85, class_id=10)

        # THEN
        self.assertEqual({"problems": ["target_grade needs class_id and target_id."]}, result["structuredContent"])
        self.assertEqual([], self.helium.requested("GET", "/planner/grades/needed/"))

    async def test_notes_for_a_class_event_or_resource(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/notes/", 200, {"count": 0, "next": None, "results": []})
        self.helium.answer("GET", "/planner/courses/", 200, [])

        # WHEN
        await self.call("list_notes", class_id=10, event_id=4, resource_id=5)

        # THEN
        params = self.helium.requested("GET", "/planner/notes/")[0].url.params
        self.assertEqual(("10", "4", "5"), (params["course"], params["event"], params["resource"]))

    async def test_assignments_in_a_category(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [])
        self.helium.answer("GET", "/planner/courses/", 200, [])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        await self.call("list_assignments", category_id=7)

        # THEN
        self.assertEqual("7", self.helium.requested("GET", "/planner/homework/")[0].url.params["category__id"])

    async def test_notes(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/notes/", 200, {
            "count": 7, "next": "https://api.heliumedu.com/planner/notes/?page=3", "results": [
                {"id": 1, "title": "Cells", "content": {"ops": [{"insert": "Mitosis\n"}]},
                 "updated_at": "2026-09-05T15:00:00Z", "course_id": 10}]})
        self.helium.answer("GET", "/planner/courses/", 200, [{"id": 10, "title": "BIO 151"}])

        # WHEN
        result = await self.call("list_notes", query="mitosis", limit=3, page=2)

        # THEN
        content = result["structuredContent"]
        self.assertEqual(("Mitosis", "BIO 151", 7, True),
                         (content["notes"][0]["text"], content["notes"][0]["class"], content["total"],
                          content["has_more"]))
        params = self.helium.requested("GET", "/planner/notes/")[0].url.params
        self.assertEqual({"include_content": "true", "search": "mitosis", "ordering": "-updated_at", "page": "2",
                          "page_size": "3"}, dict(params))

    async def test_query_without_dates_searches_the_current_terms(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200, [
            {"id": 1, "title": "Spring 2020", "start_date": "2020-01-13", "end_date": "2020-05-08"},
            {"id": 2, "title": "Far future", "start_date": "2099-01-12", "end_date": "2099-05-08"}])
        self.helium.answer("GET", "/planner/homework/", 200, [])
        self.helium.answer("GET", "/planner/courses/", 200, [])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        result = await self.call("list_assignments", query="final")

        # THEN
        content = result["structuredContent"]
        self.assertEqual(("2099-01-12", "2099-05-08"), (content["start_date"], content["end_date"]))
        self.assertEqual("final", self.helium.requested("GET", "/planner/homework/")[0].url.params["search"])

    async def test_query_for_a_class_searches_its_dates(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200, [
            {"id": 2, "title": "Far future", "start_date": "2099-01-12", "end_date": "2099-05-08"}])
        self.helium.answer("GET", "/planner/courses/", 200, [
            {"id": 7, "title": "CHEM 1770", "course_group": 1, "start_date": "2026-01-12", "end_date": "2026-05-15"}])
        self.helium.answer("GET", "/planner/homework/", 200, [
            {"id": 3, "title": "Final Exam", "course": 7, "all_day": False, "start": "2026-05-11T17:00:00Z"},
            {"id": 4, "title": "Test 1", "course": 7, "all_day": False, "start": "2026-02-18T18:05:00Z"}])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        result = await self.call("list_assignments", query="final", class_id=7)

        # THEN
        content = result["structuredContent"]
        self.assertEqual(("2026-01-12", "2026-05-15"), (content["start_date"], content["end_date"]))
        self.assertEqual(["Final Exam", "Test 1"], [item["title"] for item in content["assignments"]])
        self.assertEqual("7", self.helium.requested("GET", "/planner/homework/")[0].url.params["course__id"])

    async def test_assignments_come_in_pages(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [
            {"id": day, "title": f"Quiz {day}", "course": 10, "all_day": True,
             "start": f"2026-09-{day:02d}T05:00:00Z"} for day in range(14, 19)])
        self.helium.answer("GET", "/planner/courses/", 200, [])
        self.helium.answer("GET", "/planner/coursegroups/", 200, [])
        self.helium.answer("GET", "/planner/categories/", 200, [])

        # WHEN
        first = await self.call("list_assignments", start_date="2026-09-14", end_date="2026-09-18", limit=2)
        last = await self.call("list_assignments", start_date="2026-09-14", end_date="2026-09-18", limit=2, page=3)

        # THEN
        self.assertEqual((5, True, ["Quiz 14", "Quiz 15"]),
                         (first["structuredContent"]["total"], first["structuredContent"]["has_more"],
                          [item["title"] for item in first["structuredContent"]["assignments"]]))
        self.assertEqual((False, ["Quiz 18"]),
                         (last["structuredContent"]["has_more"],
                          [item["title"] for item in last["structuredContent"]["assignments"]]))

    async def test_event_query(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 200, [])
        self.helium.answer("GET", "/planner/events/", 200, [])

        # WHEN
        result = await self.call("list_events", query="review")

        # THEN
        content = result["structuredContent"]
        self.assertEqual(6, (date.fromisoformat(content["end_date"]) - date.fromisoformat(content["start_date"])).days)
        self.assertEqual("review", self.helium.requested("GET", "/planner/events/")[0].url.params["search"])

    async def test_resources(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/resourcegroups/", 200, [{"id": 1, "title": "Fall Textbooks"}])
        self.helium.answer("GET", "/planner/resources/", 200, [
            {"id": 5, "title": "Campbell Biology", "status": 4, "condition": 2, "website": "", "price": "$80",
             "resource_group": 1, "courses": [10]}])
        self.helium.answer("GET", "/planner/courses/", 200, [{"id": 10, "title": "BIO 151"}])

        # WHEN
        result = await self.call("list_resources")

        # THEN
        self.assertEqual([{"id": 1, "title": "Fall Textbooks", "resources": [
            {"id": 5, "title": "Campbell Biology", "status": "Need", "condition": "Used - Like New", "website": None,
             "price": "$80", "classes": [{"id": 10, "title": "BIO 151"}]}]}],
            result["structuredContent"]["resource_groups"])


class TestWellKnownFiles(McpTestCase):
    def given_well_known_files(self, files: dict[str, str]) -> Starlette:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, text in files.items():
            (Path(directory.name) / name).write_text(text)
        with mock.patch.object(app_module, "WELL_KNOWN_DIR", Path(directory.name)):
            return create_app(Settings(connector_url=CONNECTOR_URL), self.helium.transport())

    async def test_well_known_file_is_served_as_plain_text(self) -> None:
        # GIVEN
        app = self.given_well_known_files({"openai-apps-challenge": "token-from-the-portal\n"})

        # WHEN
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=CONNECTOR_URL) as client:
            response = await client.get("/.well-known/openai-apps-challenge")

        # THEN
        self.assertEqual(200, response.status_code)
        self.assertEqual("token-from-the-portal", response.text)
        self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))
        self.assertEqual([], self.helium.requests)

    async def test_only_well_known_files_are_served(self) -> None:
        # GIVEN
        app = self.given_well_known_files({"openai-apps-challenge": "token-from-the-portal", ".DS_Store": "junk"})

        # WHEN
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=CONNECTOR_URL) as client:
            responses = [await client.get(f"/.well-known/{name}") for name in (".DS_Store", "assetlinks.json")]

        # THEN
        self.assertEqual([404, 404], [response.status_code for response in responses])


class TestOriginSecret(McpTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.secured = create_app(Settings(connector_url=CONNECTOR_URL, origin_secret="cloudfront-only"),
                                  self.helium.transport())

    async def test_requests_without_the_secret_are_refused(self) -> None:
        # GIVEN
        async with httpx.AsyncClient(transport=httpx.ASGITransport(self.secured), base_url=CONNECTOR_URL) as client:
            # WHEN
            missing = await client.get("/status")
            wrong = await client.get("/status", headers={"X-Origin-Secret": "guess"})
            right = await client.get("/status", headers={"X-Origin-Secret": "cloudfront-only"})

        # THEN
        self.assertEqual((403, 403, 200), (missing.status_code, wrong.status_code, right.status_code))
        self.assertEqual("42", missing.headers["X-Muppets"])
        self.assertEqual([], self.helium.requests)
