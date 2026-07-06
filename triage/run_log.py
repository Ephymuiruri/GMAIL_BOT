"""Writing run artifacts (raw fetch snapshots and decision logs) to disk."""

import json
import os
from datetime import datetime

LOGS_DIR = "logs"


def write_snapshot(emails: list[dict], run_id: str) -> str:
    """Write the raw fetch snapshot to disk. Untouched audit record — never re-written."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"emails_{run_id}.json")
    with open(path, "w") as f:
        json.dump({"fetched_at": datetime.now().isoformat(), "emails": emails}, f, indent=2)
    return path


def write_decisions(decisions: list[dict], run_id: str) -> str:
    """Write what was decided/done for each email, separate from the raw snapshot."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"decisions_{run_id}.json")
    with open(path, "w") as f:
        json.dump({"run_at": datetime.now().isoformat(), "decisions": decisions}, f, indent=2)
    return path

