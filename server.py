"""MCP server with ephemeral channel broadcasts and interactive Codex sessions."""

import asyncio
import json
import logging
import math
import os
import re
import shlex
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import codex_sessions

_operation_logging_enabled = False
_operation_logger = logging.getLogger("koord.operations")
_operation_logger.setLevel(logging.INFO)
_operation_logger.propagate = False
if not _operation_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s\n%(message)s"))
    _operation_logger.addHandler(handler)


def _format_log_value(value) -> str:
    """Keep parameter values on one line inside a readable log block."""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _format_log_fields(fields: dict, prefix: str = "") -> list[str]:
    """Render parameter fields, including nested values, without JSON syntax."""
    lines = []
    for key, value in fields.items():
        label = f"{prefix}{_format_log_value(key)}"
        if isinstance(value, dict) and value:
            lines.extend(_format_log_fields(value, prefix=f"{label}."))
        elif isinstance(value, list) and value:
            lines.extend(_format_log_fields(dict(enumerate(value)), prefix=f"{label}."))
        else:
            if key == "message" and isinstance(value, str) and len(value) > 500:
                value = value[:500] + "[...]"
            text = "none" if value is None or value == [] or value == {} else _format_log_value(value)
            lines.append(f"== {label}: {text}")
    return lines


def _log_event(event: str, **details) -> None:
    """Write each operation or result as one readable terminal log block."""
    if not _operation_logging_enabled:
        return
    if event == "operation_invoked":
        lines = [f"** {_format_log_value(details['operation'])} **"]
        lines.extend(_format_log_fields(details["parameters"]))
    elif event == "monitor_result":
        lines = ["Statistics:"]
        statistics = details["statistics"]
        channels = statistics.get("channels", [])
        for channel in channels:
            count = channel["listeners"]
            unit = "listener" if count == 1 else "listeners"
            lines.append(f"== Channel {_format_log_value(channel['channel'])}: {count} {unit}")
        if not channels:
            lines.append("== Channels: none")
        lines.extend(_format_log_fields({key: value for key, value in statistics.items() if key != "channels"}))
    else:
        label = {
            "codex_session_created": "Codex session created",
            "message_sent": "Message sent",
        }.get(event, event.replace("_", " ").capitalize())
        lines = [f"{label}:"]
        lines.extend(_format_log_fields(details))
    _operation_logger.info("\n".join(lines))


class KoordMCPServer(MCPServer):
    """Log incoming tool operations before argument validation or execution."""

    async def call_tool(self, name: str, arguments: dict, context=None):
        _log_event("operation_invoked", operation=name, parameters=arguments)
        return await super().call_tool(name, arguments, context)


mcp = KoordMCPServer("Koord")

_listeners: dict[str, set[asyncio.Future[str]]] = {}


@dataclass(frozen=True)
class ListenResult:
    """A received message or an expired listening timeout."""

    status: Literal["message", "timeout"]
    message: str | None


@dataclass(frozen=True)
class ChannelStatus:
    """A channel name and its current number of waiting listeners."""

    channel: str
    listeners: int


@dataclass(frozen=True)
class MonitorResult:
    """A server activity snapshot that can gain additional fields over time."""

    channels: list[ChannelStatus]


@dataclass(frozen=True)
class CodexSessionResult:
    """A detached interactive session and the command used to attach to it."""

    status: Literal["started"]
    session: str
    attach_command: str
    codex_session_id: str | None = None
    resume_command: str | None = None


@dataclass(frozen=True)
class CodexSessionIdentity:
    """A captured Codex ID, or null while the startup hook has not reported it."""

    session: str
    codex_session_id: str | None
    resume_command: str | None


async def _wait_for_codex_session_id(record: Path, timeout: float = 3.0) -> str | None:
    """Wait only for startup, never for the prompt's completion."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        session_id = codex_sessions.read_launch(record)["codex_session_id"]
        if session_id is not None:
            return session_id
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.1, remaining))


@mcp.tool()
async def get_codex_session(session_name: str) -> CodexSessionIdentity:
    """Look up a new_codex launch's resumable Codex ID by its tmux session name.

    ID and resume_command are null until the SessionStart hook reports the ID.
    Records survive MCP server restarts. This does not check whether tmux or
    Codex is still running; the ID identifies the saved conversation.
    """
    try:
        data = codex_sessions.read_launch(codex_sessions.find_launch(session_name))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError(f"Could not look up Codex session {session_name!r}: {exc}") from exc
    session_id = data["codex_session_id"]
    return CodexSessionIdentity(
        session=session_name, codex_session_id=session_id,
        resume_command=f"codex resume {shlex.quote(session_id)}" if session_id else None,
    )


@mcp.tool()
async def say(channel: str, message: str) -> int:
    """Broadcast a message to all current listeners on the exact channel name.

    Return the number of listeners notified. Messages are never stored; if no
    one is listening, the message is lost. Each listen receives only one message.
    """
    delivered = 0
    for listener in _listeners.get(channel, ()):
        if not listener.done():
            listener.set_result(message)
            delivered += 1
    _log_event("message_sent", channel=channel, listeners=delivered)
    return delivered


@mcp.tool()
async def listen(channel: str, timeout: float) -> ListenResult:
    """Wait for the next message broadcast on the exact channel name.

    Timeout is a finite, non-negative number of seconds; zero returns immediately.
    Return status='message' with the message, or status='timeout' with message=null.
    Previous broadcasts are not replayed. A timeout is a normal result, not an error.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Timeout must be a finite, non-negative number of seconds.")

    listener = asyncio.get_running_loop().create_future()
    listeners = _listeners.setdefault(channel, set())
    listeners.add(listener)
    try:
        async with asyncio.timeout(timeout):
            message = await listener
        return ListenResult(status="message", message=message)
    except TimeoutError:
        return ListenResult(status="timeout", message=None)
    finally:
        listeners.discard(listener)
        if not listeners:
            del _listeners[channel]


@mcp.tool()
async def monitor() -> MonitorResult:
    """Return a JSON snapshot of channels with listeners currently waiting.

    Each channels entry contains the channel name and its listener count.
    Channels are sorted by name. Completed or cancelled listeners are excluded.
    The snapshot covers all clients connected to this server process.
    """
    channels = []
    for channel, listeners in sorted(_listeners.items()):
        count = sum(not listener.done() for listener in listeners)
        if count:
            channels.append(ChannelStatus(channel=channel, listeners=count))
    result = MonitorResult(channels=channels)
    _log_event("monitor_result", statistics=asdict(result))
    return result


@mcp.tool()
async def new_codex(
    session_name: str, prompt: str, directory: str, model: str, effort: str
) -> CodexSessionResult:
    """Start interactive Codex in a new detached tmux session with the given prompt.

    Session names must be 1-64 ASCII letters, digits, underscores or hyphens,
    starting with a letter or digit. Existing sessions are never replaced.
    Wait up to three seconds after tmux starts for Codex's session ID; otherwise
    return null and use get_codex_session later. Never wait for Codex to finish.
    Codex may require reviewing the ID capture hook with /hooks before it runs.
    Attach from another terminal on this host using the returned attach_command.
    Directory must be an existing absolute path on this server; ~ is expanded
    for the server user. Codex runs there with the server user's credentials and
    normal Codex permissions; sign-in or approval prompts remain interactive.
    Model selects the Codex model ID. Effort sets model_reasoning_effort (for
    example low, medium, high, xhigh, max or ultra); supported levels depend on
    the model and CLI version. Both values are required and passed explicitly.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", session_name):
        raise ToolError("Session name must be 1-64 letters, digits, underscores or hyphens, starting with a letter or digit.")
    if not prompt.strip() or "\0" in prompt:
        raise ToolError("Prompt must be non-empty and must not contain null characters.")
    if not model.strip() or "\0" in model:
        raise ToolError("Model must be non-empty and must not contain null characters.")
    if not effort.strip() or "\0" in effort:
        raise ToolError("Effort must be non-empty and must not contain null characters.")

    if not directory.strip() or "\0" in directory:
        raise ToolError("Directory must be a non-empty path without null characters.")
    try:
        working_directory = Path(directory).expanduser()
        if not working_directory.is_absolute():
            raise ToolError("Directory must be an absolute path on the MCP server.")
        working_directory = working_directory.resolve(strict=True)
        if not working_directory.is_dir():
            raise ToolError("Directory must point to an existing directory.")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError(f"Could not access directory: {exc}") from exc

    tmux = shutil.which("tmux")
    codex = shutil.which("codex")
    if tmux is None or codex is None:
        raise ToolError("Both tmux and codex must be installed and available on the MCP server's PATH.")

    try:
        record = codex_sessions.prepare_launch(session_name)
    except OSError as exc:
        raise ToolError(f"Could not prepare Codex session tracking: {exc}") from exc
    hook_command = shlex.join([sys.executable, str(Path(__file__).resolve().with_name("codex_session_hook.py"))])
    hook_config = (
        'hooks.SessionStart=[{matcher="^startup$",hooks=[{type="command",command='
        + json.dumps(hook_command) + ',timeout=5}]}]'
    )
    # tmux executes its single command argument through a shell; quote every word.
    command = shlex.join([
        codex, "--cd", str(working_directory), "--model", model,
        "--config", f"model_reasoning_effort={json.dumps(effort)}",
        "--config", hook_config, "--", prompt,
    ])
    try:
        process = await asyncio.create_subprocess_exec(
            tmux, "new-session", "-d", "-s", session_name,
            "-c", str(working_directory),
            "-e", f"KOORD_CODEX_SESSION_FILE={record}",
            "-x", "120", "-y", "40", command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        record.unlink(missing_ok=True)
        raise ToolError(f"Could not launch tmux: {exc}") from exc

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, TimeoutError):
            raise ToolError("Timed out waiting for tmux to create the session. Check tmux ls before retrying.") from exc
        raise
    if process.returncode != 0:
        record.unlink(missing_ok=True)
        detail = stderr.decode(errors="replace").strip()
        raise ToolError(f"Could not create tmux session: {detail}")

    _log_event(
        "codex_session_created", session_name=session_name, prompt=prompt,
        directory=str(working_directory), model=model, effort=effort,
        command=command,
    )
    try:
        codex_sessions.register_launch(session_name, record)
        session_id = await _wait_for_codex_session_id(record)
    except (OSError, ValueError, KeyError) as exc:
        raise ToolError(f"Tmux session was created, but session tracking failed: {exc}") from exc
    return CodexSessionResult(
        status="started",
        session=session_name,
        attach_command=f"tmux attach -t {session_name}",
        codex_session_id=session_id,
        resume_command=f"codex resume {shlex.quote(session_id)}" if session_id else None,
    )


def main() -> None:
    """Load local environment settings and start the MCP HTTP endpoint."""
    global _operation_logging_enabled

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    log_operations = os.getenv("MCP_LOG_OPERATIONS", "false").strip().lower()
    if log_operations not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError("MCP_LOG_OPERATIONS must be true or false (also accepts 1/0, yes/no, on/off).")
    _operation_logging_enabled = log_operations in {"true", "1", "yes", "on"}
    host = os.getenv("MCP_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("MCP_HOST must not be empty.")
    try:
        port = int(os.getenv("MCP_PORT", "8118"))
    except ValueError as exc:
        raise ValueError("MCP_PORT must be an integer between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MCP_PORT must be an integer between 1 and 65535.")
    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
