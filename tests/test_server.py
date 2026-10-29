import asyncio
import json
from datetime import date
from typing import Any

import httpx

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
        self.assertEqual({"preview_import_term", "import_term", "list_assignments", "list_events", "list_notes",
                          "list_classes", "list_resources", "get_grades"}, set(tools))
        writes = [name for name, tool in tools.items() if not tool["annotations"]["readOnlyHint"]]
        self.assertEqual(["import_term"], writes)
        for name, tool in tools.items():
            self.assertTrue(tool["title"], name)
            self.assertEqual(tool["title"], tool["annotations"]["title"], name)
            self.assertFalse(tool["annotations"]["destructiveHint"], name)
            self.assertFalse(tool["annotations"]["openWorldHint"], name)
            self.assertLessEqual(len(name), 64)


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
                      result["structuredContent"]["warnings"])
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
        self.assertIn("Credits aren't set for 'ENG 101', so they import with 0 credits.", content["warnings"])
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
        result = await self.call("import_term", plan=self.plan_arguments(), allow_duplicate_term=True)

        # THEN
        self.assertTrue(result["structuredContent"]["imported"])

    async def test_import_only_token_skips_the_duplicate_check(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/coursegroups/", 403, {"detail": "insufficient scope"})

        # WHEN
        result = await self.call("import_term", plan=self.plan_arguments())

        # THEN
        self.assertTrue(result["structuredContent"]["imported"])


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

    async def test_notes(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/notes/", 200, {
            "count": 7, "next": "https://api.heliumedu.com/planner/notes/?page=3", "results": [
                {"id": 1, "title": "Cells", "content": {"ops": [{"insert": "Mitosis\n"}]},
                 "updated_at": "2026-09-05T15:00:00Z"}]})

        # WHEN
        result = await self.call("list_notes", query="mitosis", limit=3, page=2)

        # THEN
        content = result["structuredContent"]
        self.assertEqual(("Mitosis", 7, True), (content["notes"][0]["text"], content["total"], content["has_more"]))
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
