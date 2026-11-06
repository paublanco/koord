"""Check safe tmux session creation without launching a Codex model request."""

import asyncio
import json
import shlex
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

import server


def tool_json(result):
    """The tool's value: tools return it once, as JSON in a single text block."""
    [block] = result.content
    return json.loads(block.text)


PROJECT_DIRECTORY = str(Path(server.__file__).resolve().parent)


class CodexSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        tracking = patch("codex_sessions.STATE_DIRECTORY", Path(state.name))
        tracking.start()
        self.addCleanup(tracking.stop)
        waiting = patch("server._wait_for_codex_session_id", new=AsyncMock(return_value=None))
        waiting.start()
        self.addCleanup(waiting.stop)

    async def test_returns_session_as_json_text_and_preserves_prompt(self):
        prompt = "--help; $(touch /tmp/unwanted)\nA 'quoted' prompt with `backticks`"
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        with tempfile.TemporaryDirectory(prefix="codex project ") as directory, patch(
            "server.shutil.which", side_effect=["/usr/bin/tmux", "/path with spaces/codex"]
        ), patch(
            "server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as launch:
            result = await server.mcp.call_tool("new_codex", {
                "session_name": "trial-123", "prompt": prompt, "directory": directory,
                "model": "custom-model", "effort": "high",
            })

        self.assertEqual(tool_json(result), {
            "status": "started", "session": "trial-123", "attach_command": "tmux attach -t trial-123",
            "codex_session_id": None, "resume_command": None,
        })
        arguments = launch.call_args.args
        self.assertEqual(arguments[:5], ("/usr/bin/tmux", "new-session", "-d", "-s", "trial-123"))
        self.assertEqual(arguments[arguments.index("-c") + 1], str(Path(directory).resolve()))
        command = shlex.split(arguments[-1])
        self.assertEqual(command[:7], [
            "/path with spaces/codex", "--cd", str(Path(directory).resolve()),
            "--model", "custom-model", "--config", 'model_reasoning_effort="high"',
        ])
        self.assertEqual(command[-2:], ["--", prompt])
        hook = tomllib.loads(command[8])["hooks"]["SessionStart"][0]
        self.assertEqual(hook["matcher"], "^startup$")
        self.assertIn("codex_session_hook.py", hook["hooks"][0]["command"])
        self.assertNotIn("--dangerously-bypass-hook-trust", command)
        self.assertEqual(launch.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)

    async def test_invalid_session_names_and_prompts_do_not_launch_processes(self):
        with patch("server.asyncio.create_subprocess_exec", new=AsyncMock()) as launch:
            for name in ("", "-options", "a.b", "a:b", "a b", "a;touch", "x" * 65):
                with self.subTest(name=name), self.assertRaises(ToolError):
                    await server.new_codex(name, "Hello", PROJECT_DIRECTORY, "custom-model", "high")
            for prompt in ("", "   ", "Hello\0world"):
                with self.subTest(prompt=prompt), self.assertRaises(ToolError):
                    await server.new_codex("trial", prompt, PROJECT_DIRECTORY, "custom-model", "high")
            launch.assert_not_awaited()

    async def test_missing_dependencies_report_a_tool_error(self):
        for paths in ([None, "/bin/codex"], ["/bin/tmux", None]):
            with self.subTest(paths=paths), patch("server.shutil.which", side_effect=paths):
                with self.assertRaisesRegex(ToolError, "installed"):
                    await server.new_codex("trial", "Hello", PROJECT_DIRECTORY, "custom-model", "high")

    async def test_duplicate_session_failure_is_reported_without_replacing_it(self):
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b"", b"duplicate session: trial\n")
        with patch("server.shutil.which", side_effect=["/bin/tmux", "/bin/codex"]), patch(
            "server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as launch:
            with self.assertRaisesRegex(ToolError, "duplicate session"):
                await server.new_codex("trial", "Hello", PROJECT_DIRECTORY, "custom-model", "high")
            self.assertNotIn("-A", launch.call_args.args)

    async def test_launch_timeout_reaps_the_tmux_client(self):
        process = AsyncMock()
        process.returncode = None
        process.kill = Mock()
        process.communicate.side_effect = TimeoutError()
        with patch("server.shutil.which", side_effect=["/bin/tmux", "/bin/codex"]), patch(
            "server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            with self.assertRaisesRegex(ToolError, "Timed out"):
                await server.new_codex("trial", "Hello", PROJECT_DIRECTORY, "custom-model", "high")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_invalid_directories_do_not_launch_processes(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "server.asyncio.create_subprocess_exec", new=AsyncMock()
        ) as launch:
            file = Path(directory) / "file.txt"
            file.write_text("This is a file, not a directory.")
            for path in ("", "   ", "relative/path", "bad\0path", str(file), str(Path(directory) / "missing")):
                with self.subTest(path=path), self.assertRaises(ToolError):
                    await server.new_codex("trial", "Hello", path, "custom-model", "high")
            launch.assert_not_awaited()

    async def test_model_effort_and_directory_are_required_in_mcp_schema(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertNotIn("test_codex", tools)
        self.assertEqual(set(tools["new_codex"].input_schema["required"]), {
            "session_name", "prompt", "directory", "model", "effort",
        })

    async def test_empty_or_null_model_and_effort_do_not_launch_processes(self):
        with patch("server.asyncio.create_subprocess_exec", new=AsyncMock()) as launch:
            for value in ("", "   ", "bad\0value"):
                with self.subTest(model=value), self.assertRaises(ToolError):
                    await server.new_codex("trial", "Hello", PROJECT_DIRECTORY, value, "high")
                with self.subTest(effort=value), self.assertRaises(ToolError):
                    await server.new_codex("trial", "Hello", PROJECT_DIRECTORY, "custom-model", value)
            launch.assert_not_awaited()

    async def test_model_and_effort_are_passed_literally(self):
        model = "model; $(touch /tmp/unwanted)"
        effort = 'high"\nother_setting = "changed'
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        with patch("server.shutil.which", side_effect=["/bin/tmux", "/bin/codex"]), patch(
            "server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as launch:
            await server.new_codex("trial", "Hello", PROJECT_DIRECTORY, model, effort)
        command = shlex.split(launch.call_args.args[-1])
        self.assertEqual(command[command.index("--model") + 1], model)
        config = tomllib.loads(command[command.index("--config") + 1])
        self.assertEqual(config, {"model_reasoning_effort": effort})


if __name__ == "__main__":
    unittest.main()
