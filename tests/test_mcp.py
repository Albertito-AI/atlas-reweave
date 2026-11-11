"""Real SDK stdio integration. Install the wheel with [mcp] before these tests."""
from importlib import metadata, util
import builtins
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from reweave.examples import INTEGER_EXAMPLE, RATIONAL_EXAMPLE

_HAS_MCP = util.find_spec("mcp") is not None
_TOOLS = {"list_stages", "run_registered_stage", "recheck_work", "inspect_receipt"}


class AdapterStartupTests(unittest.TestCase):
    def test_missing_optional_sdk_exits_cleanly_without_protocol_stdout(self):
        from reweave.mcp_server import main
        ordinary_import = builtins.__import__

        def without_sdk(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise ImportError("Optional SDK deliberately absent in this test")
            return ordinary_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            stdout, stderr = StringIO(), StringIO()
            with patch("builtins.__import__", side_effect=without_sdk):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as stopped:
                        main(["--store", str(Path(temporary) / "work.sqlite")])
            self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("python -m pip install '.[mcp]'", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


@unittest.skipUnless(_HAS_MCP, "Optional mcp package is not installed")
class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if metadata.version("mcp") != "2.3.0":
            raise AssertionError("The optional adapter tests require the supported mcp==2.3.0")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reweave-mcp-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = self.directory / "work.sqlite"

    def client(self, *, mode="auto", strict=False):
        from mcp import Client, StdioServerParameters
        args = ["-m", "reweave.mcp_server", "--store", str(self.store), "--scope", "mcp-test"]
        if strict:
            args.append("--integer-only")
        # No source PYTHONPATH is supplied: the subprocess must find the installed
        # distribution from a cwd outside the checkout.
        parameters = StdioServerParameters(command=sys.executable, args=args, cwd=self.directory)
        return Client(parameters, mode=mode, read_timeout_seconds=5, cache=None)

    async def run_stage(self, client, name, value, parents=None):
        result = await client.call_tool("run_registered_stage", {
            "request": {"stage_name": name, "input": value, "parents": parents or []},
        })
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def test_auto_protocol_duplicate_restart_and_current_parent_gate(self):
        import anyio
        with anyio.fail_after(30):
            async with self.client() as client:
                self.assertEqual(client.protocol_version, "2026-07-28")
                listing = await client.list_tools()
                self.assertEqual({tool.name for tool in listing.tools}, _TOOLS)
                self.assertTrue(all(tool.input_schema and tool.output_schema for tool in listing.tools))
                discovered = await client.call_tool("list_stages", {})
                self.assertFalse(discovered.is_error)
                stages = {stage["name"]: stage for stage in discovered.structured_content["stages"]}
                self.assertEqual(set(stages), {"linear.solve", "linear.aggregate", "text.excerpt"})
                self.assertEqual(stages["linear.solve"]["input_example"], INTEGER_EXAMPLE)
                integer = await self.run_stage(client, "linear.solve", INTEGER_EXAMPLE)
                duplicate = await self.run_stage(client, "linear.solve", INTEGER_EXAMPLE)
                self.assertEqual(integer["status"], "accepted")
                self.assertEqual(integer["work_key"], duplicate["work_key"])
                self.assertTrue(duplicate["generation_reused"])
                self.assertTrue(duplicate["verification_reused"])
                rational = await self.run_stage(client, "linear.solve", RATIONAL_EXAMPLE)
                child = await self.run_stage(client, "linear.aggregate", {"label": "MCP fixture"},
                                             [integer["work_key"], rational["work_key"]])
                self.assertEqual(child["status"], "accepted")
                inspected = await client.call_tool("inspect_receipt", {"request": {"work_key": child["work_key"]}})
                self.assertFalse(inspected.is_error)
                self.assertNotIn("output", inspected.structured_content)
                self.assertNotIn("status", inspected.structured_content)

            # The first child process has exited. The second owns the same durable
            # store and applies a different acceptance policy without regeneration.
            async with self.client(strict=True) as client:
                blocked = await client.call_tool("recheck_work", {"request": {"work_key": child["work_key"]}})
                self.assertFalse(blocked.is_error)
                self.assertEqual(blocked.structured_content["status"], "blocked")
                self.assertNotIn("output", blocked.structured_content)
                self.assertTrue(blocked.structured_content["generation_reused"])
                rejected = await client.call_tool("recheck_work", {"request": {"work_key": rational["work_key"]}})
                self.assertEqual(rejected.structured_content["status"], "rejected")
                self.assertNotIn("output", rejected.structured_content)
                accepted = await self.run_stage(client, "linear.solve", INTEGER_EXAMPLE)
                self.assertEqual(accepted["status"], "accepted")
                self.assertTrue(accepted["generation_reused"])

    async def test_legacy_protocol_and_narrow_error_surface(self):
        import anyio
        with anyio.fail_after(20):
            async with self.client(mode="legacy") as client:
                self.assertEqual(client.protocol_version, "2025-11-25")
                self.assertEqual({tool.name for tool in (await client.list_tools()).tools}, _TOOLS)
                valid = await self.run_stage(client, "linear.solve", INTEGER_EXAMPLE)
                invalid_requests = [
                    {"stage_name": "unknown", "input": INTEGER_EXAMPLE},
                    {"stage_name": "linear.solve", "input": INTEGER_EXAMPLE, "factory": "untrusted:code"},
                    {"stage_name": "linear.solve", "input": INTEGER_EXAMPLE, "parents": ["not-a-key"]},
                ]
                for request in invalid_requests:
                    result = await client.call_tool("run_registered_stage", {"request": request})
                    self.assertTrue(result.is_error)
                    self.assertIsNone(result.structured_content)
                missing = await client.call_tool("recheck_work", {"request": {"work_key": "0" * 64}})
                self.assertFalse(missing.is_error)
                self.assertEqual(missing.structured_content["status"], "stale")
                self.assertNotIn("output", missing.structured_content)
                inspected = await client.call_tool("inspect_receipt", {"request": {"work_key": valid["work_key"]}})
                self.assertFalse(inspected.is_error)
                self.assertNotIn("output", inspected.structured_content)


if __name__ == "__main__":
    unittest.main()
