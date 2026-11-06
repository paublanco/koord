"""Check dotenv loading, process environment precedence, and port validation."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


class EnvironmentTests(unittest.TestCase):
    def test_operation_logging_can_be_enabled_and_disabled_from_dotenv(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "server.__file__", str(Path(directory) / "server.py")
        ), patch.object(server.mcp, "run"), patch.object(server, "_operation_logging_enabled", False):
            for value, expected in (("true", True), ("false", False), ("ON", True), ("0", False)):
                with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                    (Path(directory) / ".env").write_text(f"MCP_LOG_OPERATIONS={value}\n")
                    server.main()
                    self.assertEqual(server._operation_logging_enabled, expected)

    def test_loads_dotenv_next_to_server_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch(
            "server.__file__", str(Path(directory) / "server.py")
        ), patch.object(server.mcp, "run") as run:
            (Path(directory) / ".env").write_text("MCP_HOST=0.0.0.0\nMCP_PORT=9123\n")
            server.main()
            run.assert_called_once_with(transport="streamable-http", host="0.0.0.0", port=9123)

    def test_process_environment_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MCP_HOST": "127.0.0.2", "MCP_PORT": "9124"}, clear=True
        ), patch("server.__file__", str(Path(directory) / "server.py")), patch.object(server.mcp, "run") as run:
            (Path(directory) / ".env").write_text("MCP_HOST=0.0.0.0\nMCP_PORT=9123\n")
            server.main()
            run.assert_called_once_with(transport="streamable-http", host="127.0.0.2", port=9124)

    def test_rejects_invalid_settings_before_starting_server(self):
        invalid_settings = [
            {"MCP_PORT": port} for port in ("", "abc", "1.5", "0", "-1", "65536")
        ] + [{"MCP_HOST": "   "}, {"MCP_LOG_OPERATIONS": "invalid"}]
        with patch("server.load_dotenv"), patch.object(server.mcp, "run") as run:
            for settings in invalid_settings:
                with self.subTest(settings=settings), patch.dict(os.environ, settings, clear=True):
                    with self.assertRaises(ValueError):
                        server.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
