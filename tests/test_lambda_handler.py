import json
import unittest
from typing import Any
from unittest import mock

from tests.testcase import TOKEN, USER

PROTOCOL_VERSION = "2025-11-25"


def given_a_function_url_event(method: str, path: str, body: dict[str, Any] | None = None,
                               token: str | None = TOKEN) -> dict[str, Any]:
    headers = {"host": "abc123.lambda-url.us-east-1.on.aws", "content-type": "application/json",
               "accept": "application/json, text/event-stream", "mcp-protocol-version": PROTOCOL_VERSION}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "accountId": "anonymous",
            "apiId": "abc123",
            "domainName": "abc123.lambda-url.us-east-1.on.aws",
            "domainPrefix": "abc123",
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "203.0.113.9",
                     "userAgent": "AI-User"},
            "requestId": "request-id",
            "routeKey": "$default",
            "stage": "$default",
            "time": "28/Oct/2026:12:00:00 +0000",
            "timeEpoch": 1793188800000,
        },
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


class TestLambdaHandler(unittest.TestCase):
    def test_warm_invocations_reuse_the_running_server(self) -> None:
        # GIVEN
        from helium_connector import lambda_handler
        from helium_connector.auth import HeliumTokenVerifier

        # WHEN
        with mock.patch.object(HeliumTokenVerifier, "user_for", mock.AsyncMock(return_value=USER)):
            responses = [lambda_handler.handler(given_a_function_url_event(
                "POST", "/mcp", {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}), None)
                for request_id in (1, 2, 3)]

        # THEN
        for response in responses:
            self.assertEqual(200, response["statusCode"], response)
            self.assertEqual(8, len(json.loads(response["body"])["result"]["tools"]))

    def test_unauthenticated_invocation_is_challenged(self) -> None:
        # GIVEN
        from helium_connector import lambda_handler

        # WHEN
        response = lambda_handler.handler(given_a_function_url_event(
            "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None), None)

        # THEN
        self.assertEqual(401, response["statusCode"])
        self.assertIn("resource_metadata=", response["headers"]["www-authenticate"])

    def test_status(self) -> None:
        # GIVEN
        from helium_connector import lambda_handler

        # WHEN
        response = lambda_handler.handler(given_a_function_url_event("GET", "/status", token=None), None)

        # THEN
        self.assertEqual(200, response["statusCode"])
        self.assertEqual("ok", json.loads(response["body"])["status"])
