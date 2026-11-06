"""Command-line client for Koord: listen, say and monitor from a shell.

Any environment that runs shell commands can use it. It exists for one case an MCP tool call
handles badly: an agent that must wait for a message without being blocked meanwhile. A shell
command can be started in the background at once, and its harness reports when it exits; an MCP
call blocks the agent until it returns (Claude Code moves it to the background only after two
minutes). A wait costs the agent no model step, however long it lasts.
"""

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
from pathlib import Path

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

# A wait is one listen call, never a loop of shorter ones: between two calls nobody would be
# listening, and a say in that gap would find no listener and the message would be lost. Without
# --timeout the call asks for a wait no real one reaches. The HTTP client reads the response
# stream with no read timeout (the SDK's default would end it after 300 s). If this process is
# killed, the server drops the listener as the connection closes (checked with SIGKILL, SIGTERM
# and SIGINT), so an abandoned wait leaves nothing behind.
UNBOUNDED_WAIT_SECONDS = 1e9
# Connecting and sending keep a timeout: the SDK's own default for them.
CONNECT_AND_WRITE_TIMEOUT_SECONDS = 30.0
# Since the HTTP client reads with no timeout, every step but the wait itself carries a deadline of
# its own: connecting with the MCP handshake, say and monitor, and listen --timeout beyond its
# timeout. Without it, a server that accepts the connection and never answers would hold the
# command for ever. Only listen without --timeout waits without a limit, which is its purpose.
REQUEST_DEADLINE_SECONDS = 30.0
# Closing has one too, shorter, since the command's work is done by then: on exit the transport
# asks the server to end the HTTP session (a DELETE), and a server that never confirms would
# otherwise hold the command after its output is printed. Past it, the session is left to the
# server, a warning goes to stderr, and the exit code stays the command's.
CLOSE_DEADLINE_SECONDS = 5.0
# The server accepts messages up to 4 MiB, so a listen response can be larger than the SDK's own
# default of 1 MiB per SSE event, which would reject it. The margin also covers a server that sends
# the message twice, as text and as structured content.
MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024

EXIT_OK = 0
EXIT_ERROR = 1  # the server could not be reached, or a tool call failed
EXIT_USAGE = 2  # argparse's own code for invalid arguments
EXIT_NO_MESSAGE = 3  # listen --timeout expired without a message


class KoordError(Exception):
    """A tool call that the server answered with an error."""


def server_url(environ=None, env_file=None) -> str:
    """Return the endpoint: KOORD_URL, else the server's own MCP_HOST and MCP_PORT settings.

    The settings are read as the server reads them: the process environment first, then the
    .env file beside this module. A server bound to every interface is reached on loopback:
    0.0.0.0 on 127.0.0.1, :: on ::1.
    """
    environ = os.environ if environ is None else environ
    url = environ.get("KOORD_URL", "").strip()
    if url:
        return url
    env_file = Path(__file__).resolve().with_name(".env") if env_file is None else env_file
    settings = {**dotenv_values(env_file), **environ}
    host = (settings.get("MCP_HOST") or "").strip() or "127.0.0.1"
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    port = (settings.get("MCP_PORT") or "").strip() or "8118"
    return f"http://{host}:{port}/mcp"


def tool_value(result):
    """Return a tool result's value, whether the server sent structured content or text only."""
    text = "".join(block.text for block in result.content if getattr(block, "type", None) == "text")
    if result.is_error:
        raise KoordError(text or "the tool call failed")
    data = result.structured_content
    if data is None:
        return json.loads(text)
    # The SDK wraps a non-object return value, such as say's count, as {"result": value}.
    if set(data) == {"result"}:
        return data["result"]
    return data


async def listen(client, channel: str, timeout: float | None = None) -> str | None:
    """Wait for the next message on the channel; None if `timeout` seconds pass without one.

    Without a timeout, wait until a message arrives. Either way it is a single call to the
    server's listen, so there is no moment in the wait when nobody is listening.
    """
    wait = UNBOUNDED_WAIT_SECONDS if timeout is None else timeout
    data = tool_value(await client.call_tool("listen", {"channel": channel, "timeout": wait}))
    return data["message"] if data["status"] == "message" else None


async def say(client, channel: str, message: str) -> int:
    """Broadcast the message and return how many listeners received it."""
    return int(tool_value(await client.call_tool("say", {"channel": channel, "message": message})))


async def monitor(client) -> dict:
    """Return the server's snapshot of channels with waiting listeners."""
    return tool_value(await client.call_tool("monitor", {}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="koord",
        description="Talk to a Koord server from a shell. The endpoint is --url, else KOORD_URL, "
        "else the server's MCP_HOST and MCP_PORT (process environment, then the .env beside the "
        "server), else http://127.0.0.1:8118/mcp.",
        epilog="Exit codes: 0 done; 1 server unreachable or tool error; 2 invalid arguments; "
        "3 listen --timeout expired without a message.",
    )
    parser.add_argument("--url", help="the server's MCP endpoint")
    commands = parser.add_subparsers(dest="command", required=True)

    listen_parser = commands.add_parser(
        "listen", help="wait for the next message on a channel and print it",
        description="Wait for the next message on the channel, print it followed by a newline, "
        "and exit. The wait is a single listen on the server, with no gap in which a message "
        "could be missed.",
    )
    listen_parser.add_argument("channel")
    listen_parser.add_argument(
        "--timeout", type=float,
        help="give up after this many seconds (exit 3); without it, wait until a message arrives",
    )

    say_parser = commands.add_parser(
        "say", help="broadcast a message and print how many listeners received it",
        description="Broadcast the message to the channel's current listeners and print how many "
        "received it. 0 means nobody was listening: the message is lost.",
    )
    say_parser.add_argument("channel")
    say_parser.add_argument("message", help="the message, or - to read it from standard input")

    commands.add_parser("monitor", help="print the channels with waiting listeners, as JSON")
    return parser


async def execute(args, client, stdin=None, stdout=None, stderr=None) -> int:
    """Run one parsed command against a connected client and return the exit code."""
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if args.command == "listen":
        deadline = None if args.timeout is None else args.timeout + REQUEST_DEADLINE_SECONDS
        async with asyncio.timeout(deadline):
            message = await listen(client, args.channel, args.timeout)
        if message is None:
            print(f"koord: no message on {args.channel!r} within {args.timeout:g} s", file=stderr)
            return EXIT_NO_MESSAGE
        print(message, file=stdout)
    elif args.command == "say":
        message = stdin.read() if args.message == "-" else args.message
        async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
            count = await say(client, args.channel, message)
        print(count, file=stdout)
    else:
        async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
            snapshot = await monitor(client)
        print(json.dumps(snapshot), file=stdout)
    stdout.flush()
    return EXIT_OK


def describe(exc: BaseException) -> str:
    """Name the first real error inside the task groups the transport wraps it in."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError) and not str(exc):
        return "TimeoutError: the server did not answer in time"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _connect_and_execute(args, url: str) -> int:
    http_client = create_mcp_http_client(timeout=httpx2.Timeout(CONNECT_AND_WRITE_TIMEOUT_SECONDS, read=None))
    async with http_client:  # closed here: the transport leaves a client it was given open
        transport = streamable_http_client(url, http_client=http_client, max_sse_event_size=MAX_SSE_EVENT_BYTES)
        stack = contextlib.AsyncExitStack()
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                client = await stack.enter_async_context(Client(transport))
            return await execute(args, client)
        finally:
            try:
                async with asyncio.timeout(CLOSE_DEADLINE_SECONDS):
                    await stack.aclose()
            except TimeoutError:
                print(f"koord: the server did not confirm closing the session within "
                      f"{CLOSE_DEADLINE_SECONDS:g} s; left to the server ({url})", file=sys.stderr)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "listen" and args.timeout is not None and (
        not math.isfinite(args.timeout) or args.timeout < 0
    ):
        parser.error("--timeout must be a finite, non-negative number of seconds")
    url = args.url or server_url()
    try:
        return asyncio.run(_connect_and_execute(args, url))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - every failure becomes one line and exit 1
        print(f"koord: {describe(exc)} ({url})", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
