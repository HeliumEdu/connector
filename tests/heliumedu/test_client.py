from datetime import date

import httpx

from heliumedu.client import ICS_FILE_NAME, IMPORT_FILE_NAME, HeliumClient
from heliumedu.exception import (
    HeliumAuthError,
    HeliumConnectionError,
    HeliumNotFoundError,
    HeliumPermissionError,
    HeliumRateLimitedError,
    HeliumValidationError,
)
from tests.testcase import IMPORT_COUNTS, TOKEN, USER, AsyncTestCase, uploaded_import


class HeliumClientTestCase(AsyncTestCase):
    async def asyncSetUp(self) -> None:
        self.session = httpx.AsyncClient(transport=self.helium.transport())

    async def asyncTearDown(self) -> None:
        await self.session.aclose()

    def given_client(self, token: str = TOKEN, **options: str) -> HeliumClient:
        return HeliumClient(self.session, token, "https://api.example.test/", **options)


class TestGetUser(HeliumClientTestCase):
    async def test_get_user(self) -> None:
        # GIVEN
        client = self.given_client()

        # WHEN
        user = await client.get_user()

        # THEN
        self.assertEqual(USER, user)
        request = self.helium.requested("GET", "/auth/user/")[0]
        self.assertEqual(f"Bearer {TOKEN}", request.headers["Authorization"])
        self.assertEqual("api.example.test", request.url.host)

    async def test_user_agent_names_the_package_then_the_caller(self) -> None:
        # GIVEN
        client = self.given_client(agent="helium-connector/1.0.0")

        # WHEN
        await client.get_user()

        # THEN
        self.assertEqual("heliumedu helium-connector/1.0.0",
                         self.helium.requests[0].headers["User-Agent"])

    async def test_refused_token(self) -> None:
        # GIVEN
        client = self.given_client(token="revoked")

        # WHEN / THEN
        with self.assertRaises(HeliumAuthError):
            await client.get_user()


class TestImportData(HeliumClientTestCase):
    async def test_import_uploads_one_json_file(self) -> None:
        # GIVEN
        client = self.given_client()
        data = {"course_groups": [{"id": 1, "title": "Fall 2026"}]}

        # WHEN
        result = await client.import_data(data)

        # THEN
        request = self.helium.requested("POST", "/importexport/import/")[0]
        self.assertIn(f'name="file[]"; filename="{IMPORT_FILE_NAME}"', request.read().decode("utf-8"))
        self.assertEqual(data, uploaded_import(request))
        self.assertNotIn("dry_run", request.url.params)
        self.assertEqual(IMPORT_COUNTS, result.counts)
        self.assertEqual({}, result.ids)
        self.assertFalse(result.dry_run)

    async def test_dry_run(self) -> None:
        # GIVEN
        client = self.given_client()

        # WHEN
        result = await client.import_data({}, dry_run=True)

        # THEN
        self.assertEqual("true", self.helium.requested("POST", "/importexport/import/")[0].url.params["dry_run"])
        self.assertTrue(result.dry_run)

    async def test_import_ics_into_a_class(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/ics/", 200, {**IMPORT_COUNTS, "homework": 4})
        client = self.given_client()

        # WHEN
        result = await client.import_ics("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", course_id=52, dry_run=True)

        # THEN
        request = self.helium.requested("POST", "/importexport/import/ics/")[0]
        body = request.read().decode("utf-8")
        self.assertIn(f'name="file[]"; filename="{ICS_FILE_NAME}"', body)
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn('name="target_type"\r\n\r\ncourse', body)
        self.assertIn('name="course"\r\n\r\n52', body)
        self.assertEqual("true", request.url.params["dry_run"])
        self.assertEqual(4, result.counts["homework"])
        self.assertTrue(result.dry_run)

    async def test_import_ics_as_events(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/ics/", 200, IMPORT_COUNTS)
        client = self.given_client()

        # WHEN
        await client.import_ics("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

        # THEN
        request = self.helium.requested("POST", "/importexport/import/ics/")[0]
        body = request.read().decode("utf-8")
        self.assertIn('name="target_type"\r\n\r\nevents', body)
        self.assertNotIn('name="course"', body)
        self.assertNotIn("dry_run", request.url.params)

    async def test_import_returns_assigned_ids(self) -> None:
        # GIVEN
        self.helium.answer("POST", "/importexport/import/", 200,
                           {**IMPORT_COUNTS, "ids": {"course_groups": {"1": 412}}})
        client = self.given_client()

        # WHEN
        result = await client.import_data({})

        # THEN
        self.assertEqual({"course_groups": {"1": 412}}, result.ids)
        self.assertNotIn("ids", result.counts)

    async def test_rejected_import_carries_helium_errors(self) -> None:
        # GIVEN
        errors = {"homework": ["homework id 3, start: Datetime must include a timezone."]}
        self.helium.answer("POST", "/importexport/import/", 400, errors)
        client = self.given_client()

        # WHEN / THEN
        with self.assertRaises(HeliumValidationError) as raised:
            await client.import_data({})
        self.assertEqual({"errors": errors}, raised.exception.meta)


class TestListing(HeliumClientTestCase):
    async def test_homework_filters(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [])
        client = self.given_client()

        # WHEN
        await client.list_homework(date(2026, 9, 1), date(2026, 9, 8), course_ids=[3, 4], completed=False,
                                   category_ids=[7, 8])

        # THEN
        params = self.helium.requested("GET", "/planner/homework/")[0].url.params
        self.assertEqual("2026-09-01", params["from"])
        self.assertEqual("2026-09-08", params["to"])
        self.assertEqual("3,4", params["course__id"])
        self.assertEqual("false", params["completed"])
        self.assertEqual("start", params["ordering"])
        self.assertEqual("7,8", params["category__id"])
        self.assertNotIn("overdue", params)
        self.assertNotIn("search", params)

    async def test_search(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/homework/", 200, [])
        self.helium.answer("GET", "/planner/events/", 200, [])
        client = self.given_client()

        # WHEN
        await client.list_homework(search="final")
        await client.list_events(date(2026, 9, 1), date(2026, 9, 8), search="review")

        # THEN
        self.assertEqual("final", self.helium.requested("GET", "/planner/homework/")[0].url.params["search"])
        self.assertEqual({"from": "2026-09-01", "to": "2026-09-08", "search": "review"},
                         dict(self.helium.requested("GET", "/planner/events/")[0].url.params))

    async def test_resources(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/resourcegroups/", 200, [{"id": 1, "title": "Fall Textbooks"}])
        self.helium.answer("GET", "/planner/resources/", 200, [{"id": 5, "title": "Campbell Biology"}])
        client = self.given_client()

        # WHEN
        groups, resources = await client.list_resource_groups(), await client.list_resources()

        # THEN
        self.assertEqual(["Fall Textbooks"], [group["title"] for group in groups])
        self.assertEqual(["Campbell Biology"], [resource["title"] for resource in resources])

    async def test_window_needs_both_ends(self) -> None:
        # GIVEN
        client = self.given_client()

        # WHEN / THEN
        with self.assertRaises(ValueError):
            await client.list_events(date(2026, 9, 1))
        self.assertEqual([], self.helium.requests)

    async def test_notes_include_content(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/notes/", 200, [])
        client = self.given_client()

        # WHEN
        await client.list_notes("mitosis", homework_id=9)

        # THEN
        params = self.helium.requested("GET", "/planner/notes/")[0].url.params
        self.assertEqual({"include_content": "true", "search": "mitosis", "homework": "9"}, dict(params))

    async def test_notes_filters(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/notes/", 200, [])
        client = self.given_client()

        # WHEN
        await client.list_notes(course_id=3, event_id=4, resource_id=5)

        # THEN
        params = self.helium.requested("GET", "/planner/notes/")[0].url.params
        self.assertEqual({"include_content": "true", "course": "3", "event": "4", "resource": "5"}, dict(params))

    async def test_needed_grade(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/grades/needed/", 200, {"state": "achievable", "needed_grade": 77.5})
        client = self.given_client()

        # WHEN
        needed = await client.get_needed_grade(10, 2, 85)

        # THEN
        self.assertEqual({"state": "achievable", "needed_grade": 77.5}, needed)
        params = self.helium.requested("GET", "/planner/grades/needed/")[0].url.params
        self.assertEqual({"course": "10", "target": "2", "desired_grade": "85"}, dict(params))

    async def test_notes_page_past_the_last(self) -> None:
        # GIVEN
        pages = {"1": httpx.Response(200, json={"count": 3, "next": None, "results": [{"id": 1}, {"id": 2}]}),
                 "2": httpx.Response(404, json={"detail": "Invalid page."})}
        transport = httpx.MockTransport(lambda request: pages[request.url.params["page"]])

        # WHEN
        async with httpx.AsyncClient(transport=transport) as session:
            page = await HeliumClient(session, TOKEN, "https://api.example.test/").list_notes_page(page=2, page_size=5)

        # THEN
        self.assertEqual(([], 3, False), (page.items, page.total, page.has_more))

    async def test_notes_page(self) -> None:
        # GIVEN
        self.helium.answer("GET", "/planner/notes/", 200,
                           {"count": 12, "next": None, "previous": "...", "results": [{"id": 4, "title": "Cells"}]})
        client = self.given_client()

        # WHEN
        page = await client.list_notes_page("mitosis", page=3, page_size=5)

        # THEN
        self.assertEqual(([{"id": 4, "title": "Cells"}], 12, False), (page.items, page.total, page.has_more))
        params = self.helium.requested("GET", "/planner/notes/")[0].url.params
        self.assertEqual({"include_content": "true", "search": "mitosis", "ordering": "-updated_at", "page": "3",
                          "page_size": "5"}, dict(params))


class TestErrors(HeliumClientTestCase):
    async def test_status_codes(self) -> None:
        # GIVEN
        client = self.given_client()
        cases = [(403, HeliumPermissionError), (404, HeliumNotFoundError), (500, HeliumConnectionError),
                 (409, HeliumValidationError)]

        for status, error in cases:
            with self.subTest(status=status):
                self.helium.answer("GET", "/planner/grades/", status, {"detail": "no"})

                # WHEN / THEN
                with self.assertRaises(error):
                    await client.get_grades()

    async def test_rate_limited(self) -> None:
        # GIVEN
        session = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "12"})))
        client = HeliumClient(session, TOKEN)

        # WHEN / THEN
        with self.assertRaises(HeliumRateLimitedError) as raised:
            await client.get_grades()
        self.assertEqual({"retry_after": 12}, raised.exception.meta)
        await session.aclose()

    async def test_unreachable(self) -> None:
        # GIVEN
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        session = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        client = HeliumClient(session, TOKEN)

        # WHEN / THEN
        with self.assertRaises(HeliumConnectionError):
            await client.get_user()
        await session.aclose()
