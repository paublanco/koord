"""Capture the session ID supplied by Codex's SessionStart command hook."""

import json
import os
from pathlib import Path
import sys

from codex_sessions import capture_session_id


def main() -> None:
    record = os.getenv("KOORD_CODEX_SESSION_FILE")
    if record:
        capture_session_id(Path(record), json.load(sys.stdin))


if __name__ == "__main__":
    main()
