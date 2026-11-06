"""Check the command-line client: one listen call per wait, both result shapes, the endpoint and exit codes."""

import asyncio
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp import Client

import koord_cli
import server


def text_result(text, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], structured_content=None, is_error=is_error)


class CliAgainstServerTests(unittest.IsolatedAsyncioTestCase):
    # Each test opens its own in-process client: anyio requires a client to be closed by the
    # task that opened it, and asyncSetUp and the cleanups run in different tasks.

    async def wait_for_listener(self, channel):
        for _ in range(200):
            if server._listeners.get(channel):
                return
            await asyncio.sleep(0.01)
        self.fail(f"nobody started listening on {channel!r}")

    async def run_command(self, *argv, stdin=""):
        args = koord_cli.build_parser().parse_args(list(argv))
        stdout, stderr = io.StringIO(), io.StringIO()
        code = await koord_cli.execute(args, self.client, io.StringIO(stdin), stdout, stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    async def test_listen_returns_the_message_said_meanwhile(self):
        async with Client(server.mcp) as self.client:
            waiting = asyncio.create_task(koord_cli.listen(self.client, "general", timeout=2))
            await self.wait_for_listener("general")
            self.assertEqual(await server.say("general", "Hello!\nSecond line"), 1)
            self.assertEqual(await waiting, "Hello!\nSecond line")

    async def test_a_wait_is_one_listen_call_so_no_message_falls_in_a_gap(self):
        async with Client(server.mcp) as self.client:
            calls = []
            original = self.client.call_tool

            async def recording_call_tool(name, arguments=None, *rest, **options):
                calls.append((name, arguments))
                return await original(name, arguments, *rest, **options)

            with patch.object(self.client, "call_tool", recording_call_tool):
                waiting = asyncio.create_task(koord_cli.listen(self.client, "general"))
                await self.wait_for_listener("general")
                await asyncio.sleep(0.2)
                self.assertEqual(await server.say("general", "Late"), 1)
                self.assertEqual(await waiting, "Late")
            self.assertEqual(calls, [("listen", {"channel": "general", "timeout": koord_cli.UNBOUNDED_WAIT_SECONDS})])

    async def test_listen_gives_up_at_its_timeout_and_leaves_no_listener(self):
        async with Client(server.mcp) as self.client:
            self.assertIsNone(await koord_cli.listen(self.client, "general", timeout=0.1))
            self.assertNotIn("general", server._listeners)

    async def test_say_and_monitor_return_plain_values(self):
        async with Client(server.mcp) as self.client:
            waiting = asyncio.create_task(koord_cli.listen(self.client, "general", timeout=2))
            await self.wait_for_listener("general")
            self.assertEqual(await koord_cli.monitor(self.client), {"channels": [{"channel": "general", "listeners": 1}]})
            self.assertEqual(await koord_cli.say(self.client, "general", "Hi"), 1)
            await waiting
            self.assertEqual(await koord_cli.say(self.client, "general", "Lost"), 0)

    async def test_listen_command_prints_the_message_and_exits_0(self):
        async with Client(server.mcp) as self.client:
            waiting = asyncio.create_task(self.run_command("listen", "general", "--timeout", "2"))
            await self.wait_for_listener("general")
            await server.say("general", "EOT")
            self.assertEqual(await waiting, (0, "EOT\n", ""))

    async def test_listen_command_exits_3_when_its_timeout_expires(self):
        async with Client(server.mcp) as self.client:
            code, stdout, stderr = await self.run_command("listen", "general", "--timeout", "0.05")
            self.assertEqual((code, stdout), (koord_cli.EXIT_NO_MESSAGE, ""))
            self.assertIn("no message on 'general'", stderr)

    async def test_say_command_reads_the_message_from_stdin_with_a_dash(self):
        async with Client(server.mcp) as self.client:
            waiting = asyncio.create_task(koord_cli.listen(self.client, "general", timeout=2))
            await self.wait_for_listener("general")
            self.assertEqual(await self.run_command("say", "general", "-", stdin="From stdin\n"), (0, "1\n", ""))
            self.assertEqual(await waiting, "From stdin\n")

    async def test_after_the_handshake_every_call_but_an_unbounded_listen_has_a_deadline(self):
        async def never_answers(*_args, **_options):
            await asyncio.Event().wait()

        async with Client(server.mcp) as self.client:
            with patch.object(koord_cli, "REQUEST_DEADLINE_SECONDS", 0.2), patch.object(
                self.client, "call_tool", never_answers
            ):
                for argv in (["say", "general", "m"], ["monitor"], ["listen", "general", "--timeout", "0.1"]):
                    with self.subTest(argv=argv), self.assertRaises(TimeoutError):
                        await self.run_command(*argv)
                unbounded = asyncio.create_task(self.run_command("listen", "general"))
                await asyncio.sleep(0.5)
                self.assertFalse(unbounded.done())
                unbounded.cancel()
                await asyncio.gather(unbounded, return_exceptions=True)

    async def test_monitor_command_prints_json(self):
        async with Client(server.mcp) as self.client:
            code, stdout, _ = await self.run_command("monitor")
            self.assertEqual((code, json.loads(stdout)), (0, {"channels": []}))


class ToolValueTests(unittest.TestCase):
    def test_reads_text_content_when_there_is_no_structured_content(self):
        self.assertEqual(koord_cli.tool_value(text_result('{"status": "message", "message": "Hi"}')),
                         {"status": "message", "message": "Hi"})
        self.assertEqual(koord_cli.tool_value(text_result("2")), 2)

    def test_unwraps_a_structured_scalar_result(self):
        result = SimpleNamespace(content=[], structured_content={"result": 3}, is_error=False)
        self.assertEqual(koord_cli.tool_value(result), 3)

    def test_a_tool_error_raises_with_its_text(self):
        with self.assertRaisesRegex(koord_cli.KoordError, "Timeout must be"):
            koord_cli.tool_value(text_result("Timeout must be finite", is_error=True))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"

    def url(self, environ, dotenv=""):
        self.env_file.write_text(dotenv)
        return koord_cli.server_url(environ, self.env_file)

    def test_defaults_to_the_local_server(self):
        self.assertEqual(self.url({}), "http://127.0.0.1:8118/mcp")

    def test_koord_url_wins(self):
        self.assertEqual(self.url({"KOORD_URL": "http://example:1/mcp", "MCP_PORT": "9"}), "http://example:1/mcp")

    def test_process_environment_overrides_the_server_dotenv(self):
        self.assertEqual(self.url({}, "MCP_HOST=127.0.0.3\nMCP_PORT=9123\n"), "http://127.0.0.3:9123/mcp")
        self.assertEqual(self.url({"MCP_PORT": "9124"}, "MCP_PORT=9123\n"), "http://127.0.0.1:9124/mcp")

    def test_a_server_bound_to_every_interface_is_reached_on_loopback(self):
        self.assertEqual(self.url({"MCP_HOST": "0.0.0.0"}), "http://127.0.0.1:8118/mcp")
        self.assertEqual(self.url({"MCP_HOST": "::"}), "http://[::1]:8118/mcp")
        self.assertEqual(self.url({"MCP_HOST": "::1"}), "http://[::1]:8118/mcp")


class SessionServer:
    """A minimal session-based MCP endpoint in a thread: it answers initialize, tools/list and
    tools/call (monitor), and DELETE only if told to. Enough to watch how the client closes."""

    def __init__(self, answer_delete):
        released, self.deletes = threading.Event(), []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, body=None, headers=()):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                for name, value in headers:
                    self.send_header(name, value)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in request:
                    return self.reply(202)
                method, answer = request["method"], {"jsonrpc": "2.0", "id": request["id"]}
                if method == "initialize":
                    answer["result"] = {"protocolVersion": request["params"]["protocolVersion"],
                                        "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
                    return self.reply(200, answer, [("mcp-session-id", "s1")])
                if method == "tools/list":
                    answer["result"] = {"tools": [{"name": "monitor", "inputSchema": {"type": "object"}}]}
                elif method == "tools/call":
                    answer["result"] = {"content": [{"type": "text", "text": '{"channels": []}'}], "isError": False}
                else:
                    answer["error"] = {"code": -32601, "message": "Method not found"}
                self.reply(200, answer)

            def do_GET(self):
                self.reply(405)

            def do_DELETE(self):
                owner.deletes.append(self.headers.get("mcp-session-id"))
                if answer_delete:
                    return self.reply(200)
                released.wait(10)  # never answers while the test runs

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        self._released = released
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self._released.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class MainTests(unittest.TestCase):
    def run_main(self, *argv, limit=10):
        """Run the command in a thread, so that one that never ends fails the test instead of hanging it."""
        stdout, stderr, outcome = io.StringIO(), io.StringIO(), {}

        def run():
            with redirect_stdout(stdout), patch("sys.stderr", stderr):
                outcome["code"] = koord_cli.main(list(argv))

        started = time.monotonic()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(limit)
        if worker.is_alive():
            self.fail(f"koord {' '.join(argv)} still running after {limit} s; output so far: {stdout.getvalue()!r}")
        return outcome["code"], stdout.getvalue(), stderr.getvalue(), time.monotonic() - started

    def test_a_server_that_never_confirms_closing_does_not_hold_the_command(self):
        fake = SessionServer(answer_delete=False)
        self.addCleanup(fake.close)
        with patch.object(koord_cli, "CLOSE_DEADLINE_SECONDS", 0.3):
            code, stdout, stderr, elapsed = self.run_main("--url", fake.url, "monitor")
        self.assertEqual((code, json.loads(stdout)), (0, {"channels": []}))
        self.assertIn("did not confirm closing the session", stderr)
        self.assertEqual(fake.deletes, ["s1"])
        self.assertLess(elapsed, 5)

    def test_a_server_that_confirms_closing_gives_no_warning(self):
        fake = SessionServer(answer_delete=True)
        self.addCleanup(fake.close)
        code, stdout, stderr, _ = self.run_main("--url", fake.url, "monitor")
        self.assertEqual((code, json.loads(stdout), stderr), (0, {"channels": []}, ""))
        self.assertEqual(fake.deletes, ["s1"])

    def test_an_unreachable_server_exits_1_with_one_line(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(koord_cli.main(["--url", f"http://127.0.0.1:{port}/mcp", "monitor"]), 1)
        self.assertRegex(stderr.getvalue(), rf"^koord: .+ \(http://127\.0\.0\.1:{port}/mcp\)\n$")

    def test_a_server_that_never_answers_ends_every_command_with_exit_1(self):
        # Bound and listening, so the connection is accepted, but nothing ever reads or answers.
        with socket.socket() as silent, patch.object(koord_cli, "REQUEST_DEADLINE_SECONDS", 0.3):
            silent.bind(("127.0.0.1", 0))
            silent.listen(8)
            url = f"http://127.0.0.1:{silent.getsockname()[1]}/mcp"
            for argv in (["monitor"], ["say", "general", "m"], ["listen", "general", "--timeout", "0.1"], ["listen", "general"]):
                stderr = io.StringIO()
                with self.subTest(argv=argv), patch("sys.stderr", stderr):
                    self.assertEqual(koord_cli.main(["--url", url, *argv]), 1)
                    self.assertIn("did not answer in time", stderr.getvalue())

    def test_a_negative_or_infinite_timeout_is_a_usage_error(self):
        for value in ("-1", "inf", "nan"):
            with self.subTest(value=value), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as raised:
                koord_cli.main(["listen", "general", "--timeout", value])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
