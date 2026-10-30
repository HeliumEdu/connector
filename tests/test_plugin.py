import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

import jsonschema

from helium_connector import __version__
from helium_connector.settings import DEFAULT_CONNECTOR_URL, MCP_PATH
from tests.test_server import McpTestCase

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = Path(__file__).resolve().parent / "schemas"
#: The categories OpenAI's plugin directory accepts.
CATEGORIES = {"Productivity", "Creativity", "Developer Tools", "Business & Operations", "Data & Analytics",
              "Communication", "Education & Research", "Security", "Finance", "Healthcare", "Travel",
              "Entertainment", "Other"}


def given_the_packaged_plugin() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "helium-plugin.zip"
        subprocess.run([sys.executable, str(ROOT / "bin" / "package-plugin.py"), str(output)], check=True,
                       capture_output=True)
        with zipfile.ZipFile(output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}


def openai(files: dict[str, bytes]) -> dict[str, Any]:
    extension: dict[str, Any] = json.loads(files["plugin.json"])["extensions"]["com.openai"]
    return extension


class TestPluginPackage(unittest.TestCase):
    def test_package_is_a_valid_agent_plugin(self) -> None:
        # WHEN
        files = given_the_packaged_plugin()

        # THEN
        manifest = json.loads(files["plugin.json"])
        jsonschema.validate(manifest, json.loads((SCHEMAS / "agent-plugins-plugin.schema.json").read_text()))
        jsonschema.validate(json.loads(files["mcp.json"]),
                            json.loads((SCHEMAS / "agent-plugins-mcp.schema.json").read_text()))
        self.assertEqual(__version__, manifest["version"])
        self.assertEqual({"plugin.json", "mcp.json", "assets/logo.png"}, set(files))

    def test_mcp_server_is_the_connector(self) -> None:
        # WHEN
        files = given_the_packaged_plugin()

        # THEN
        servers = json.loads(files["mcp.json"])["mcpServers"]
        self.assertEqual({"helium": {"type": "streamable-http", "url": f"{DEFAULT_CONNECTOR_URL}{MCP_PATH}"}},
                         servers)

    def test_listing_fits_the_directory(self) -> None:
        # WHEN
        listing = openai(given_the_packaged_plugin())["interface"]

        # THEN
        self.assertLessEqual(len(listing["displayName"]), 30)
        self.assertLessEqual(len(listing["shortDescription"]), 30)
        self.assertLessEqual(len(listing["longDescription"]), 4000)
        self.assertLessEqual(len(listing["developerName"]), 80)
        self.assertIn(listing["category"], CATEGORIES)
        self.assertLessEqual(len(listing["capabilities"]), 20)
        self.assertTrue(all(len(capability) <= 120 for capability in listing["capabilities"]))
        self.assertLessEqual(len(listing["defaultPrompt"]), 3)
        self.assertEqual(len(listing["defaultPrompt"]), len(set(listing["defaultPrompt"])))
        self.assertTrue(all(len(prompt) <= 128 for prompt in listing["defaultPrompt"]))
        for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
            self.assertTrue(listing[field].startswith("https://"), field)

    def test_icons_are_square_and_packaged(self) -> None:
        # GIVEN
        files = given_the_packaged_plugin()
        listing = openai(files)["interface"]

        # WHEN
        icons = {listing["logo"], listing["composerIcon"]}

        # THEN
        for icon in icons:
            png = files[icon.removeprefix("./")]
            width, height = struct.unpack(">II", png[16:24])
            self.assertEqual(width, height, icon)
            self.assertGreaterEqual(width, 48, icon)

    def test_package_carries_no_reviewer_credentials(self) -> None:
        # WHEN
        review = openai(given_the_packaged_plugin())["review"]

        # THEN
        self.assertNotIn("test_credentials", review)
        self.assertNotIn("reviewer_instructions", review)


class TestPluginReviewCases(McpTestCase):
    async def test_review_cases_use_the_servers_tools(self) -> None:
        # GIVEN
        cases = openai(given_the_packaged_plugin())["review"]["test_cases"]

        # WHEN
        response = await self.rpc("tools/list")

        # THEN
        tools = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertEqual((5, 3), (len(cases["positive"]), len(cases["negative"])))
        for case in cases["positive"]:
            triggered = {name.strip() for name in case["tools_triggered"].split(",")}
            self.assertLessEqual(triggered, tools, case["description"])
            self.assertTrue(case["expected_behavior"], case["description"])
        for case in cases["negative"]:
            self.assertTrue(case["prompt"], case["description"])
