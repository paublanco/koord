"""Check broadcast delivery, timeouts, and listener cleanup."""

import asyncio
import unittest

import server


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

    async def test_tools_publish_structured_listen_schema(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(set(tools), {"say", "listen", "monitor", "new_codex", "get_codex_session"})
        schema = tools["listen"].output_schema
        self.assertEqual(schema["properties"]["status"]["enum"], ["message", "timeout"])
        self.assertEqual(set(schema["required"]), {"status", "message"})

    async def test_mcp_calls_return_structured_results(self):
        waiting = asyncio.create_task(server.mcp.call_tool("listen", {"channel": "general", "timeout": 1}))
        self.addAsyncCleanup(self.stop_listener, waiting)
        await asyncio.sleep(0)
        sent = await server.mcp.call_tool("say", {"channel": "general", "message": "Hello over MCP!"})
        self.assertEqual(sent.structured_content, {"result": 1})
        received = await waiting
        self.assertEqual(received.structured_content, {"status": "message", "message": "Hello over MCP!"})
        expired = await server.mcp.call_tool("listen", {"channel": "general", "timeout": 0})
        self.assertEqual(expired.structured_content, {"status": "timeout", "message": None})
        self.assertFalse(expired.is_error)

    async def test_monitor_returns_empty_json_list_without_listeners(self):
        result = await server.mcp.call_tool("monitor", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"channels": []})

    async def test_monitor_counts_listeners_per_channel_in_name_order(self):
        await self.start_listener("updates")
        await self.start_listener("general")
        await self.start_listener("general")
        result = await server.mcp.call_tool("monitor", {})
        self.assertEqual(result.structured_content, {
            "channels": [
                {"channel": "general", "listeners": 2},
                {"channel": "updates", "listeners": 1},
            ]
        })
        await server.say("general", "Hello!")
        current = await server.monitor()
        self.assertEqual(current.channels, [server.ChannelStatus("updates", 1)])
        self.assertEqual(len(result.structured_content["channels"]), 2)

    async def test_monitor_excludes_cancelled_and_timed_out_listeners(self):
        cancelled = await self.start_listener("cancelled")
        cancelled.cancel()
        self.assertEqual((await server.monitor()).channels, [])
        await server.listen("expired", 0.01)
        self.assertEqual((await server.monitor()).channels, [])

    async def test_monitor_publishes_json_output_schema(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        schema = tools["monitor"].output_schema
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["channels"]["type"], "array")


if __name__ == "__main__":
    unittest.main()
