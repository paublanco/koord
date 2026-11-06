"""Persist the association between tmux launches and Codex session IDs."""

import json
import os
from pathlib import Path
import re
from uuid import uuid4

STATE_DIRECTORY = Path(__file__).resolve().parent / ".koord" / "sessions"


def validate_session_name(session_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", session_name):
        raise ValueError("Session name must be 1-64 letters, digits, underscores or hyphens, starting with a letter or digit.")


def write_json(path: Path, data: dict) -> None:
    """Atomically publish a complete record for readers in other processes."""
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_launch(session_name: str) -> Path:
    validate_session_name(session_name)
    STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    record = STATE_DIRECTORY / f"launch-{uuid4().hex}.json"
    write_json(record, {"session": session_name, "codex_session_id": None})
    return record


def register_launch(session_name: str, record: Path) -> None:
    """Replace a name association only after tmux successfully creates it."""
    validate_session_name(session_name)
    write_json(STATE_DIRECTORY / f"{session_name}.json", {"launch": record.name})


def read_launch(record: Path) -> dict:
    return json.loads(record.read_text(encoding="utf-8"))


def find_launch(session_name: str) -> Path:
    validate_session_name(session_name)
    index = read_launch(STATE_DIRECTORY / f"{session_name}.json")
    name = index["launch"]
    if not re.fullmatch(r"launch-[0-9a-f]{32}\.json", name):
        raise ValueError("Invalid stored launch reference.")
    return STATE_DIRECTORY / name


def capture_session_id(record: Path, event: dict) -> None:
    """Save only the ID of the root session's initial startup event."""
    if event.get("hook_event_name") != "SessionStart" or event.get("source") != "startup":
        return
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
        raise ValueError("Codex did not provide a valid session_id.")
    data = read_launch(record)
    if data.get("codex_session_id") is None:
        data["codex_session_id"] = session_id
        write_json(record, data)
