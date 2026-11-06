"""Check terminal events and the operation logging toggle."""

import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

import server


def tool_json(result):
    """The tool's value: tools return it once, as JSON in a single text block."""
    [block] = result.content
    return json.loads(block.text)


class OperationLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        tracking = patch("codex_sessions.STATE_DIRECTORY", Path(state.name))
        tracking.start()
        self.addCleanup(tracking.stop)
        waiting = patch("server._wait_for_codex_session_id", new=AsyncMock(return_value=None))
        waiting.start()
        self.addCleanup(waiting.stop)
        enabled = patch.object(server, "_operation_logging_enabled", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    async def test_calls_log_parameters_and_separate_delivery_and_statistics_events(self):
        message = "Hello!\nA second line"
        with self.assertLogs("koord.operations", level="INFO") as logs:
            waiting = asyncio.create_task(server.mcp.call_tool("listen", {"channel": "general", "timeout": 1}))
            self.addAsyncCleanup(self.stop_listener, waiting)
            await asyncio.sleep(0)
            await server.mcp.call_tool("monitor", {})
            await server.mcp.call_tool("say", {"channel": "general", "message": message})
            await waiting
            await server.mcp.call_tool("say", {"channel": "general", "message": "Lost"})
        events = [record.getMessage() for record in logs.records]
        self.assertEqual([event.splitlines()[0] for event in events if event.startswith("** ")], [
            "** listen **", "** monitor **", "** say **", "** say **",
        ])
        self.assertEqual(events[0], "** listen **\n== channel: general\n== timeout: 1")
        terminal_output = server._operation_logger.handlers[0].format(logs.records[0])
        self.assertRegex(terminal_output.splitlines()[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}$")
        self.assertEqual(terminal_output.splitlines()[1], "** listen **")
        self.assertEqual(events[2], "Statistics:\n== Channel general: 1 listener")
        self.assertEqual(events[3], "** say **\n== channel: general\n== message: Hello!\\nA second line")
        self.assertEqual([event for event in events if event.startswith("Message sent:")], [
            "Message sent:\n== channel: general\n== listeners: 1",
            "Message sent:\n== channel: general\n== listeners: 0",
        ])

    async def stop_listener(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_call_is_logged_before_validation(self):
        with self.assertLogs("koord.operations", level="INFO") as logs:
            with self.assertRaises(ToolError):
                await server.mcp.call_tool("listen", {"channel": "general", "timeout": "invalid"})
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].getMessage(), "** listen **\n== channel: general\n== timeout: invalid")

    async def test_large_message_log_is_truncated_but_delivery_is_complete(self):
        message = "à" * (1024 * 1024)
        with self.assertLogs("koord.operations", level="INFO") as logs:
            waiting = asyncio.create_task(server.mcp.call_tool("listen", {"channel": "general", "timeout": 1}))
            self.addAsyncCleanup(self.stop_listener, waiting)
            await asyncio.sleep(0)
            await server.mcp.call_tool("say", {"channel": "general", "message": message})
            result = await waiting
        self.assertEqual(tool_json(result)["message"], message)
        invocation = logs.records[1].getMessage()
        self.assertEqual(invocation, "** say **\n== channel: general\n== message: " + "à" * 500 + "[...]")

    async def test_message_log_boundary_and_newline_escaping(self):
        for message in ("", "a" * 499, "a" * 500, "a" * 499 + "\nZ"):
            with self.subTest(length=len(message)), self.assertLogs("koord.operations", level="INFO") as logs:
                await server.mcp.call_tool("say", {"channel": "general", "message": message})
            expected = message[:500].replace("\n", "\\n") + ("[...]" if len(message) > 500 else "")
            self.assertIn("== message: " + expected, logs.records[0].getMessage().splitlines())

    async def test_created_session_logs_all_parameters_after_success_only(self):
        parameters = {
            "session_name": "trial", "prompt": "Say hello.\nWait for input.",
            "directory": str(Path(server.__file__).resolve().parent),
            "model": "custom-model", "effort": "high",
        }
        for returncode in (0, 1):
            process = AsyncMock()
            process.returncode = returncode
            process.communicate.return_value = (b"", b"duplicate session: trial")
            with self.subTest(returncode=returncode), patch(
                "server.shutil.which", side_effect=["/bin/tmux", "/bin/codex"]
            ), patch("server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), self.assertLogs(
                "koord.operations", level="INFO"
            ) as logs:
                if returncode:
                    with self.assertRaises(ToolError):
                        await server.mcp.call_tool("new_codex", parameters)
                else:
                    await server.mcp.call_tool("new_codex", parameters)
            events = [record.getMessage() for record in logs.records]
            self.assertTrue(events[0].startswith("** new_codex **\n"))
            for key, value in parameters.items():
                expected_value = value.replace("\n", "\\n")
                self.assertIn(f"== {key}: {expected_value}", events[0].splitlines())
            self.assertEqual(len(events), 1 if returncode else 2)
            if not returncode:
                self.assertTrue(events[1].startswith("Codex session created:\n"))
                for key, value in parameters.items():
                    expected_value = value.replace("\n", "\\n")
                    self.assertIn(f"== {key}: {expected_value}", events[1].splitlines())
                self.assertIn("--model custom-model", events[1])

    async def test_disabling_logs_suppresses_invocations_and_extra_events(self):
        with patch.object(server, "_operation_logging_enabled", False), self.assertNoLogs("koord.operations"):
            await server.mcp.call_tool("say", {"channel": "general", "message": "Hello"})
            await server.mcp.call_tool("monitor", {})
            server._log_event("codex_session_created", session_name="trial")


if __name__ == "__main__":
    unittest.main()
