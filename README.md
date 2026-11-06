# Koord MCP

A Python MCP server with ephemeral channel broadcasts and interactive Codex sessions.

## Tools

### say

`say(channel: str, message: str)` broadcasts a message to every client currently
waiting on that channel. It returns the number of listeners notified, as JSON text
(`2`, for example).

Messages are not buffered or stored. A message sent without any listeners is
lost. Channel names are case-sensitive and matched exactly. Empty messages are
valid.

### listen

`listen(channel: str, timeout: float)` waits for the next message on the channel.
The timeout is required and measured in seconds; it must be finite and
non-negative. A timeout of zero returns immediately.

The result is a JSON object:

| Outcome | Result |
| --- | --- |
| Message received | `{"status": "message", "message": "Hello!"}` |
| Empty message received | `{"status": "message", "message": ""}` |
| Timeout expired | `{"status": "timeout", "message": null}` |

Use `status` to distinguish delivery from timeout. A timeout is a normal result,
not an MCP tool error. Invalid arguments produce a tool error.

Every tool returns its result once, as JSON in a single text content block, with
no structured content and no output schema (`structured_output=False`). With
structured output the SDK would send each result twice, as structured content and
as the same JSON in text, and a client that shows its model both, as Codex does,
would double the size of every message a listener receives. The tool
descriptions, which say what each tool returns, are published through
`tools/list`.

Each call receives at most one message. Call `listen` again to wait for another
one. All listeners already waiting when `say` runs receive the same message;
future listeners do not receive earlier broadcasts. Waiting is asynchronous,
so other clients can call tools while a listener waits. Timed-out and cancelled
listeners are removed.

Channel state exists only in the running Python process. Run one server process
so all clients share the same channels; restarting it discards active listeners.

To try it, use two clients or two independent Codex conversations:

1. In client A, call `listen` with `{"channel": "general", "timeout": 30}`.
2. While A is waiting, in client B call `say` with
   `{"channel": "general", "message": "Hello from client B!"}`.
3. Client A receives
   `{"status": "message", "message": "Hello from client B!"}`.
4. Call `listen` again without sending anything to observe `status: "timeout"`.

Set any client-side tool/request deadline longer than the requested listening
timeout; otherwise the client may stop waiting before the server returns its
timeout result.

### Large messages

The current SDK accepts HTTP request bodies up to 4 MiB by default. This applies
to the complete serialized request, including JSON escaping and protocol fields,
not just the message text. A count of characters is not a byte count.

`listen` returns the complete message once, as JSON text. A 1 MiB ASCII message
therefore produces a response slightly larger than 1 MiB. (Before results became
text only, the same message produced a response slightly larger than 2 MiB; in a
local check through the MCP HTTP application that exchange completed in about
20 ms, measuring server processing without an external client's network or model
work.)

Client limits differ. The [Python SDK client](https://py.sdk.modelcontextprotocol.io/client/transports/#larger-sse-events)
defaults to a 1 MiB SSE event limit, which can reject that response. Python
clients can raise `max_sse_event_size` on `streamable_http_client`. The
[Claude Code documentation](https://code.claude.com/docs/en/mcp#mcp-output-limits-and-warnings)
currently describes a 16 MiB HTTP/SSE response limit and saving large tool
results to files; its behavior should not be inferred from the Python client's
limit. The exact client error and version are needed to diagnose a failure.

Terminal operation logging uses synchronous writes before tool execution. If
the terminal or its output pipe stops accepting data, a log write can stall
the server's event loop. Message previews are capped at 500 characters to reduce
the amount written; the actual transmitted message is not truncated.

### monitor

`monitor()` takes no arguments and returns a JSON snapshot of channels with
listeners currently waiting, including the number of listeners on each channel:

```json
{
  "channels": [
    {"channel": "general", "listeners": 2},
    {"channel": "updates", "listeners": 1}
  ]
}
```

Only channels with at least one active listener are included, sorted by name.
Listeners that have already received a message, been cancelled, or timed out are
excluded. With no active listeners, the result is `{"channels": []}`.

The snapshot covers all clients connected to this server process. It does not
create a listener or consume any messages. The top-level object keeps
the `channels` list under its own key so future monitoring data can be added
as separate fields.

While another client is waiting in `listen`, ask Codex:

```text
Call monitor on the koord MCP server and show the JSON result.
```

### new_codex

`new_codex(session_name: str, prompt: str, directory: str, model: str, effort: str)` creates a new detached
`tmux` session running interactive `codex` in the supplied directory with the
initial prompt. After `tmux` creates the session, it waits up to three seconds
for Codex to report its resumable conversation ID. It never waits for Codex to
complete the prompt:

```json
{
  "status": "started",
  "session": "codex-trial",
  "attach_command": "tmux attach -t codex-trial",
  "codex_session_id": "019a1234-5678-7000-8000-123456789abc",
  "resume_command": "codex resume 019a1234-5678-7000-8000-123456789abc"
}
```

Session names must contain 1-64 ASCII letters, digits, underscores or hyphens,
starting with a letter or digit. The prompt must not be blank or contain null
characters. Existing sessions are never replaced; duplicate names produce a tool
error. The prompt is passed literally, including quotes and shell characters.

Both `tmux` and `codex` must be on the MCP server's `PATH`. In this environment,
both are already installed. On a new Ubuntu installation, install `tmux` with
`sudo apt install tmux` and follow the [official Codex CLI setup instructions](https://learn.chatgpt.com/docs/cli).

`directory` is required and must point to an existing directory on the MCP
server. Use an absolute Linux path, such as `/home/pau/projecs/koord`; `~` is
expanded to the server user's home directory. Relative paths, missing paths and
files are rejected before starting tmux. Paths containing spaces are supported.
When the server runs in WSL, use the WSL path (for example `/mnt/c/Projects/app`),
rather than a Windows path such as `C:\Projects\app`.

Both the tmux working directory and Codex's `--cd` option use this directory.
`model` and `effort` are required, non-empty strings. The launcher selects the
model using `--model` and sets the effort using
`--config 'model_reasoning_effort="high"'` (with the requested value instead of
`high`). Effort is serialized as a TOML string and all command arguments are
shell-quoted. Neither value changes the user's saved configuration.

Effort values such as `low`, `medium`, `high`, `xhigh`, `max` or `ultra` depend on
the selected model and CLI version. The launcher passes the requested values
without maintaining a fixed model/effort catalog. Codex handles model access and
compatibility when it starts. These command-line overrides are documented in
the [official OpenAI settings guide](https://learn.chatgpt.com/docs/developer-settings#change-settings-for-one-run)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Codex otherwise uses the server user's normal configuration, credentials and permissions.
It remains interactive:
authentication, workspace trust and approval prompts can require your input after
attaching. `started` confirms session creation, not Codex authentication or task
completion. Startup errors are visible in the terminal; if Codex exits, tmux
normally closes the session.

For example, ask your MCP client:

```text
Call new_codex on koord with session_name "codex-trial",
directory "/home/pau/projecs/koord", model "gpt-6.1-sol", effort "high", and prompt
"Say hello and wait for my next instruction. Do not modify files or run commands."
```

Choose a model and effort available to your Codex account and CLI. `new_codex`
replaces the previous `test_codex` tool; restart the MCP server and reconnect
your client to refresh the tool name and its required parameters.

From another terminal in the same WSL distribution, as the same Linux user:

```bash
tmux attach -t codex-trial
```

Press **Ctrl+B**, then **D** to detach and leave Codex running. Run the same
attach command to return. Use `tmux ls` to list sessions. To close this session
and stop its processes:

```bash
tmux kill-session -t codex-trial
```

The session is independent of the MCP request and survives MCP server restarts
while the WSL environment remains running.

### Capturing the Codex session ID

`session` identifies the tmux terminal. `codex_session_id` identifies the Codex
conversation used by `codex resume`; these are different identifiers.

The launcher supplies a command-line `SessionStart` hook that records Codex's
`session_id`. Each launch has a separate record, so concurrent sessions in the
same directory do not get mixed up. Existing user/project hook configuration
files are not edited. See the [official hooks documentation](https://learn.chatgpt.com/docs/hooks).

Codex requires reviewing and trusting a new command hook before it runs.
Attach to the tmux session, open `/hooks`, review the Koord capture hook and
trust it. Hook trust is kept by Codex for subsequent launches with the same
hook definition. If startup was already skipped while untrusted, trusting it
does not retroactively replay that startup event; use a subsequent launch to
capture its ID. Hooks must also be enabled in Codex and allowed by its policies.

If the hook has not reported an ID within three seconds, both `codex_session_id`
and `resume_command` are `null`. This can happen during startup, authentication,
workspace trust, or hook review. A captured ID does not guarantee model access or
successful prompt completion.

### get_codex_session

`get_codex_session(session_name: str)` retrieves the captured ID after a delayed
startup, without starting or resuming another Codex process:

```text
Call get_codex_session on koord with session_name "codex-trial".
```

Example result:

```json
{
  "session": "codex-trial",
  "codex_session_id": "019a1234-5678-7000-8000-123456789abc",
  "resume_command": "codex resume 019a1234-5678-7000-8000-123456789abc"
}
```

While capture is pending, the last two fields remain `null`. Unknown names return
a tool error. This lookup reports identity, not whether the process is running.
Only launches created with this version of Koord have tracking records.

Records are stored in `.koord/sessions/`, ignored by Git, and survive MCP server
restarts. Reusing a tmux session name after closing it points the lookup at the
new launch. A failed duplicate creation preserves the previous association.
Do not remove the records while waiting for a startup hook to report its ID.

## Setup

Python 3.11 or later is required. This environment has Python 3.12.3 installed
at `/usr/bin/python3`, running on Ubuntu 24.04.

This workspace already has `.venv` set up with pip and the dependencies;
you can skip directly to running the server. Follow the steps below to set up
a new environment.

If your Ubuntu installation lacks support for virtual environments with pip:

```bash
sudo apt update
sudo apt install python3-venv
```

From the project root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Dependencies are installed inside `.venv`.

## Environment configuration

Host and port are loaded with [python-dotenv](https://github.com/theskumar/python-dotenv).
For a new checkout, copy the example configuration from the project root:

```bash
cp .env.example .env
```

Edit `.env` to configure the server:

```dotenv
MCP_HOST=127.0.0.1
MCP_PORT=8118
MCP_LOG_OPERATIONS=true
```

`.env.example` is intended for version control; `.env` and other `.env.*` files
are ignored by Git, except `.env.example`. This workspace already has `.env`.
The server loads the file next to `server.py`, regardless of the current working
directory. Existing process environment variables take precedence over `.env`.
If a variable is absent, the defaults are `127.0.0.1` and `8118`.

`MCP_HOST` must not be empty. `MCP_PORT` must be an integer from 1 to 65535.
Use `127.0.0.1` for local connections or `0.0.0.0` to listen on all IPv4
interfaces. Clients connect using a reachable server address, not `0.0.0.0`.
Restart the server after changing `.env`. If you change the port, also update
the URL in your MCP clients. The connection examples below use the default values.

### Terminal operation logs

Set `MCP_LOG_OPERATIONS=true` to log every incoming tool call with its operation
name and supplied parameters, including calls rejected by argument validation.
Set it to `false` to disable these operation logs. The example and local `.env`
enable them; if the variable is absent, they are disabled. Values `1/0`, `yes/no`
and `on/off` are also accepted, case-insensitively.

Each event is written as a readable block to standard error in the server
terminal. The first line has the timestamp, the next line highlights the method
as `** method_name **`, followed by one `== parameter: value` line per parameter:

```text
2026-11-05 12:00:00,000
** say **
== channel: general
== message: Hello!
2026-11-05 12:00:00,001
Message sent:
== channel: general
== listeners: 2
2026-11-05 12:00:00,002
** monitor **
2026-11-05 12:00:00,003
Statistics:
== Channel general: 2 listeners
```

Additional blocks record:

- `Codex session created`: all creation parameters, the resolved directory and
  the exact command, after tmux successfully creates the session.
- `Message sent`: the channel and the number of listeners notified, including zero.
- `Statistics`: the channels and listener counts returned by `monitor`. With no
  active listeners, this displays `== Channels: none`.

Message parameters show at most their first 500 characters, followed by `[...]`
only when truncated. This affects logs only; listeners receive the complete
message. Truncation happens before escaping embedded newlines, so each parameter
stays on one line. Other parameters, including Codex prompts, retain their full
values. Each complete block is emitted as one log record
to keep concurrent operations from interleaving their parameter lines.
This setting controls Koord operation logs; SDK and
HTTP server logs keep their existing settings. Restart the server after changing it.

## Running the server

```bash
.venv/bin/python server.py
```

After installation, you can also run `.venv/bin/koord-mcp`.

With the example settings, the MCP endpoint is `http://127.0.0.1:8118/mcp`, using **Streamable HTTP**.
Configure this URL in your MCP client, connect, and call `monitor` without
arguments to inspect active listeners. The server listens on the local machine.

To try it in a visual interface, with Node.js available:

```bash
npx -y @modelcontextprotocol/inspector
```

In the Inspector, select `Streamable HTTP`, enter the URL, connect,
and open `Tools` to run `monitor` without arguments.

Built with the [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/).

## Command-line client

`koord` talks to a running server from a shell, so anything that can run a
command can use the channels: a terminal, a script, or an agent.

```bash
koord listen review-1                  # wait for the next message, print it, exit
koord listen review-1 --timeout 600    # give up after ten minutes
koord say review-1 "Round 2 is ready"  # prints how many listeners received it
koord say review-1 - < report.md       # read the message from standard input
koord monitor                          # channels with waiting listeners, as JSON
```

It exists for one case that an MCP tool call handles badly: an agent that has
to wait for a message without being blocked while it waits. A shell command can
start in the background at once, and the agent's harness reports when it exits.
An MCP call blocks the agent until it returns (Claude Code moves a call to the
background only after two minutes). A long wait costs the agent no model steps.

A wait is a single call to the server's `listen`, never a loop of shorter ones:
between two calls nobody would be listening, and a `say` in that gap would find
no listener and the message would be lost. Without `--timeout` the call asks for
a wait no real one reaches, and the client reads the response with no read
timeout (the SDK client's default would end it after 300 seconds). If the
`koord` process is killed, the server drops its listener as the connection
closes (checked with `SIGKILL`, `SIGTERM` and `SIGINT`), so an abandoned wait
leaves no listener behind.

Every other step has a deadline of 30 seconds: connecting with the MCP
handshake, `say`, `monitor`, and `listen --timeout` beyond its timeout. A server
that accepts the connection and never answers ends the command with exit code 1
instead of holding it. Only `listen` without `--timeout` waits without a limit.

Closing has a deadline too, of 5 seconds. On exit the transport may ask the
server to end the HTTP session (a `DELETE`); if the server never confirms, the
command still ends, with a warning on stderr and the exit code of the command
itself, since its output is already printed. The HTTP client is closed
explicitly.

`listen` prints the message followed by a newline. Without `--timeout` it waits
until a message arrives. It reads a result from its JSON text, or from
structured content where a server sends it.

| Exit code | Meaning |
| --- | --- |
| 0 | Message received, or command done |
| 1 | Server unreachable or not answering in time, or the tool call failed (one line on stderr) |
| 2 | Invalid arguments |
| 3 | `listen --timeout` expired without a message |

The endpoint is `--url`, else `KOORD_URL`, else the server's own `MCP_HOST` and
`MCP_PORT` (process environment first, then the `.env` beside the server), else
`http://127.0.0.1:8118/mcp`. A server bound to every interface is reached on
loopback: `0.0.0.0` on `127.0.0.1`, `::` on `::1`.
The client accepts SSE events up to 16 MiB, so a large message is not rejected
by the SDK's default 1 MiB limit (see "Large messages").

Installing the project (`.venv/bin/python -m pip install -e .`) creates
`.venv/bin/koord`. To call it from any directory, link it into a directory on
your `PATH`:

```bash
ln -s "$PWD/.venv/bin/koord" ~/.local/bin/koord
```

Before installation, run it as `.venv/bin/python koord_cli.py`.

## Connecting from the Codex Windows client

This workspace runs in WSL2. The steps below assume Codex runs on the same
Windows computer and can reach the WSL server through localhost.

### Start the server in WSL

Open a WSL terminal and run:

```bash
cd /home/pau/projecs/koord
.venv/bin/python server.py
```

Keep this terminal running. With HTTP transport, Codex connects to the running
server; it does not start the Python process for you. Restart the process after
changing Python files.

From Windows PowerShell, check that the port is reachable:

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 8118
```

`TcpTestSucceeded` should be `True`. This checks TCP connectivity; the tool call
below verifies MCP itself. If the check fails, confirm the server is running
and that Windows-to-WSL localhost forwarding is available before configuring Codex.

### Add the MCP server

In the Windows app, open **Settings → MCP servers → Add server** and enter:

| Field | Value |
| --- | --- |
| Name | `koord` |
| Transport | `Streamable HTTP` |
| URL | `http://127.0.0.1:8118/mcp` |

Save and select **Restart**. This example server does not require authentication.
These steps follow the [official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

Alternatively, add this table to the Windows app's configuration file, normally
`%USERPROFILE%\.codex\config.toml`, then restart the app:

```toml
[mcp_servers.koord]
url = "http://127.0.0.1:8118/mcp"
```

Preserve existing configuration and update the table if `mcp_servers.koord`
already exists. The Windows app and the WSL CLI use separate configuration
directories by default; editing `/home/pau/.codex/config.toml` alone does not
configure the Windows app. See the [official Windows configuration guidance](https://learn.chatgpt.com/docs/windows/windows-app#share-config-auth-and-sessions-with-wsl).

### Call the tool

Start a new conversation and type `/mcp` to check that `koord` is connected.
Then send:

```text
Use the koord MCP server to call monitor and show its JSON result.
```

The tool returns `{"channels": []}` when no clients are listening; otherwise it
lists the active channels and their listener counts. Check that Codex actually
calls the MCP tool in its activity.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover fan-out, channel isolation, discarded messages, empty messages,
timeouts, cancellation, monitoring snapshots, session creation, literal prompt
handling, and that every tool returns its result once, as JSON text with no
output schema. Codex session unit tests mock the
launcher and do not send model requests. They also verify the requested working
directory and reject invalid directories before launching a process.
Logging tests verify the environment toggle, invocation parameters, separate
detail blocks, and readable parameter output for multiline messages.
Session ID tests run the actual capture hook with synthetic startup input and
cover delayed lookup, parallel launches, reused names, and duplicate failures.
Command-line client tests connect to the server in-process and cover a wait
being a single `listen` call, giving up at `--timeout`, the deadlines on every
other step (including a server that never answers), closing against a server that
never confirms the end of the session, reading a message from standard
input, both result shapes, endpoint resolution, and exit codes.
