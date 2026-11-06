"""Check broadcast delivery, timeouts, and listener cleanup."""

import asyncio
import json
import unittest

import server


def tool_json(result):
    """The tool's value: tools return it once, as JSON in a single text block."""
    [block] = result.content
    return json.loads(block.text)


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def start_listener(self, channel="general", timeout=1):
        task = asyncio.create_task(server.listen(channel, timeout))
        self.addAsyncCleanup(self.stop_listener, task)
        await asyncio.sleep(0)
        return task

    async def stop_listener(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_broadcast_reaches_every_current_listener_once(self):
        first = await self.start_listener()
        second = await self.start_listener()
        self.assertEqual(await server.say("general", "Hello!"), 2)
        self.assertEqual(await server.say("general", "Too late"), 0)
        for task in (first, second):
            self.assertEqual(await task, server.ListenResult("message", "Hello!"))
        self.assertNotIn("general", server._listeners)

    async def test_channels_are_isolated_and_case_sensitive(self):
        listener = await self.start_listener()
        self.assertEqual(await server.say("General", "Wrong channel"), 0)
        self.assertFalse(listener.done())
        self.assertEqual(await server.say("general", "Correct channel"), 1)
        self.assertEqual((await listener).message, "Correct channel")

    async def test_messages_without_listeners_are_lost(self):
        self.assertEqual(await server.say("general", "Lost"), 0)
        result = await server.listen("general", 0.01)
        self.assertEqual(result, server.ListenResult("timeout", None))
        self.assertNotIn("general", server._listeners)

    async def test_empty_message_is_not_a_timeout(self):
        listener = await self.start_listener()
        await server.say("general", "")
        self.assertEqual(await listener, server.ListenResult("message", ""))

    async def test_timeout_does_not_remove_other_listeners(self):
        remaining = await self.start_listener()
        self.assertEqual((await server.listen("general", 0.01)).status, "timeout")
        self.assertEqual(await server.say("general", "Still listening"), 1)
        self.assertEqual((await remaining).message, "Still listening")

    async def test_cancellation_removes_listener(self):
        listener = await self.start_listener()
        listener.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await listener
        self.assertNotIn("general", server._listeners)
        self.assertEqual(await server.say("general", "Lost"), 0)

    async def test_zero_timeout_returns_immediately(self):
        self.assertEqual(await server.listen("general", 0), server.ListenResult("timeout", None))
        self.assertNotIn("general", server._listeners)

    async def test_invalid_timeouts_do_not_register_listeners(self):
        for timeout in (-1, float("inf"), float("-inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await server.listen("general", timeout)
        self.assertNotIn("general", server._listeners)

    async def test_tools_publish_no_output_schema(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(set(tools), {"say", "listen", "monitor", "new_codex", "get_codex_session"})
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertIsNone(tool.output_schema)

    async def test_mcp_calls_return_each_result_once_as_json_text(self):
        waiting = asyncio.create_task(server.mcp.call_tool("listen", {"channel": "general", "timeout": 1}))
        self.addAsyncCleanup(self.stop_listener, waiting)
        await asyncio.sleep(0)
        sent = await server.mcp.call_tool("say", {"channel": "general", "message": "Hello over MCP!"})
        self.assertIsNone(sent.structured_content)
        self.assertEqual(tool_json(sent), 1)
        received = await waiting
        self.assertIsNone(received.structured_content)
        self.assertEqual(tool_json(received), {"status": "message", "message": "Hello over MCP!"})
        expired = await server.mcp.call_tool("listen", {"channel": "general", "timeout": 0})
        self.assertEqual(tool_json(expired), {"status": "timeout", "message": None})
        self.assertFalse(expired.is_error)

    async def test_monitor_returns_empty_json_list_without_listeners(self):
        result = await server.mcp.call_tool("monitor", {})
        self.assertFalse(result.is_error)
        self.assertEqual(tool_json(result), {"channels": []})

    async def test_monitor_counts_listeners_per_channel_in_name_order(self):
        await self.start_listener("updates")
        await self.start_listener("general")
        await self.start_listener("general")
        result = await server.mcp.call_tool("monitor", {})
        self.assertEqual(tool_json(result), {
            "channels": [
                {"channel": "general", "listeners": 2},
                {"channel": "updates", "listeners": 1},
            ]
        })
        await server.say("general", "Hello!")
        current = await server.monitor()
        self.assertEqual(current.channels, [server.ChannelStatus("updates", 1)])
        self.assertEqual(len(tool_json(result)["channels"]), 2)

    async def test_monitor_excludes_cancelled_and_timed_out_listeners(self):
        cancelled = await self.start_listener("cancelled")
        cancelled.cancel()
        self.assertEqual((await server.monitor()).channels, [])
        await server.listen("expired", 0.01)
        self.assertEqual((await server.monitor()).channels, [])


if __name__ == "__main__":
    unittest.main()
