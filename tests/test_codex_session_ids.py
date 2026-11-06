"""Check launch-specific capture, delayed lookup, and persistent session IDs."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

import codex_sessions
import server


def tool_json(result):
    """The tool's value: tools return it once, as JSON in a single text block."""
    [block] = result.content
    return json.loads(block.text)


class CodexSessionIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        tracking = patch.object(codex_sessions, "STATE_DIRECTORY", self.state)
        tracking.start()
        self.addCleanup(tracking.stop)

    def event(self, session_id="019a1234-5678-7000-8000-123456789abc"):
        return {"hook_event_name": "SessionStart", "source": "startup", "session_id": session_id}

    async def test_real_hook_process_captures_id_for_later_mcp_lookup(self):
        record = codex_sessions.prepare_launch("trial")
        codex_sessions.register_launch("trial", record)
        pending = await server.mcp.call_tool("get_codex_session", {"session_name": "trial"})
        self.assertIsNone(tool_json(pending)["codex_session_id"])
        hook = Path(server.__file__).with_name("codex_session_hook.py")
        subprocess.run(
            [sys.executable, str(hook)], input=json.dumps(self.event()), text=True, check=True,
            env={**os.environ, "KOORD_CODEX_SESSION_FILE": str(record)}, capture_output=True,
        )
        result = await server.mcp.call_tool("get_codex_session", {"session_name": "trial"})
        session_id = self.event()["session_id"]
        self.assertEqual(tool_json(result), {
            "session": "trial", "codex_session_id": session_id,
            "resume_command": f"codex resume {session_id}",
        })

    async def test_new_codex_returns_id_without_waiting_for_prompt_completion(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")

        async def launch(*arguments, **kwargs):
            value = arguments[arguments.index("-e") + 1]
            record = Path(value.split("=", 1)[1])
            codex_sessions.capture_session_id(record, self.event())
            return process

        with patch("server.shutil.which", side_effect=["/bin/tmux", "/bin/codex"]), patch(
            "server.asyncio.create_subprocess_exec", side_effect=launch
        ):
            result = await server.new_codex("trial", "Hello", str(self.state), "custom-model", "high")
        self.assertEqual(result.codex_session_id, self.event()["session_id"])
        self.assertEqual(result.resume_command, f"codex resume {result.codex_session_id}")

    async def test_bounded_wait_allows_late_id_lookup(self):
        record = codex_sessions.prepare_launch("trial")
        codex_sessions.register_launch("trial", record)
        self.assertIsNone(await asyncio.wait_for(server._wait_for_codex_session_id(record, timeout=0.01), timeout=0.5))
        codex_sessions.capture_session_id(record, self.event())
        result = await server.get_codex_session("trial")
        self.assertEqual(result.codex_session_id, self.event()["session_id"])

    async def test_parallel_launches_and_reused_names_do_not_mix_ids(self):
        first = codex_sessions.prepare_launch("trial")
        second = codex_sessions.prepare_launch("other")
        codex_sessions.register_launch("trial", first)
        codex_sessions.register_launch("other", second)
        codex_sessions.capture_session_id(first, self.event("first-id"))
        codex_sessions.capture_session_id(second, self.event("second-id"))
        self.assertEqual((await server.get_codex_session("trial")).codex_session_id, "first-id")
        self.assertEqual((await server.get_codex_session("other")).codex_session_id, "second-id")
        replacement = codex_sessions.prepare_launch("trial")
        codex_sessions.register_launch("trial", replacement)
        self.assertIsNone((await server.get_codex_session("trial")).codex_session_id)
        codex_sessions.capture_session_id(replacement, self.event("new-id"))
        self.assertEqual((await server.get_codex_session("trial")).codex_session_id, "new-id")

    async def test_capture_ignores_resume_and_does_not_replace_original_id(self):
        record = codex_sessions.prepare_launch("trial")
        event = self.event()
        event["source"] = "resume"
        codex_sessions.capture_session_id(record, event)
        self.assertIsNone(codex_sessions.read_launch(record)["codex_session_id"])
        codex_sessions.capture_session_id(record, self.event("original-id"))
        codex_sessions.capture_session_id(record, self.event("different-id"))
        self.assertEqual(codex_sessions.read_launch(record)["codex_session_id"], "original-id")

    async def test_unknown_or_invalid_session_names_report_tool_errors(self):
        for name in ("unknown", "../outside"):
            with self.subTest(name=name), self.assertRaises(ToolError):
                await server.get_codex_session(name)

    async def test_failed_duplicate_launch_preserves_previous_lookup(self):
        original = codex_sessions.prepare_launch("trial")
        codex_sessions.register_launch("trial", original)
        codex_sessions.capture_session_id(original, self.event("original-id"))
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b"", b"duplicate session: trial")
        with patch("server.shutil.which", side_effect=["/bin/tmux", "/bin/codex"]), patch(
            "server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            with self.assertRaises(ToolError):
                await server.new_codex("trial", "Hello", str(self.state), "custom-model", "high")
        self.assertEqual((await server.get_codex_session("trial")).codex_session_id, "original-id")
        self.assertEqual(len(list(self.state.glob("launch-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
