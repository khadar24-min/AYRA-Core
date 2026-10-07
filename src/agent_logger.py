"""Structured logging and audit trail for AYRA autonomous agent runs.

Maintains an append-only JSONL run log at %LOCALAPPDATA%\\Jarvis\\agent_runs.jsonl
(or ~/.local/share/Jarvis/agent_runs.jsonl on Linux/fallback).

Safety invariants:
- All secrets, API keys, and auth headers are scrubbed before writing.
- Never raises exceptions that could break the agent execution loop.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def _get_log_path() -> Path:
    """Determine the audit log file path."""
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA")
        if base:
            dir_path = Path(base) / "Jarvis"
        else:
            dir_path = Path.home() / "AppData" / "Local" / "Jarvis"
    else:
        dir_path = Path.home() / ".local" / "share" / "Jarvis"

    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / "agent_runs.jsonl"


# Sensitive parameter scrubbing
_SECRET_PATTERN = re.compile(
    r"(key|token|password|secret|auth|bearer|passphrase|credential)[\"']?\s*[:=]\s*[\"']?([^\"',\s]+)",
    re.IGNORECASE,
)


def _scrub_data(obj: Any) -> Any:
    """Recursively scrub sensitive keys and token patterns."""
    if isinstance(obj, dict):
        scrubbed = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(term in k_lower for term in ("key", "token", "password", "secret", "auth", "credential")):
                scrubbed[k] = "[REDACTED]"
            else:
                scrubbed[k] = _scrub_data(v)
        return scrubbed
    elif isinstance(obj, list):
        return [_scrub_data(item) for item in obj]
    elif isinstance(obj, str):
        if len(obj) > 2000:
            obj = obj[:2000] + "... [truncated]"
        return _SECRET_PATTERN.sub(r"\1: [REDACTED]", obj)
    return obj


class AgentLogger:
    """Structured audit logger for autonomous agent execution."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self.log_path = log_path or _get_log_path()

    def log_run_event(
        self,
        *,
        goal: str,
        task_id: str,
        step_id: int,
        domain: str,
        tool: str,
        args: Dict[str, Any],
        result: Any,
        verified: bool,
        error: Optional[str] = None,
        recovery_attempt: Optional[str] = None,
        final_status: str = "in_progress",
    ) -> None:
        """Record one structured action event to agent_runs.jsonl."""
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "task_id": task_id,
            "goal": goal,
            "step_id": step_id,
            "domain": domain,
            "tool": tool,
            "args": _scrub_data(args),
            "result_summary": _scrub_data(str(result)[:500] if result is not None else None),
            "verified": verified,
            "error": error,
            "recovery_attempt": recovery_attempt,
            "final_status": final_status,
        }
        try:
            line = json.dumps(record, ensure_ascii=False)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            print(f"[agent_logger] write failure: {exc}", file=sys.stderr)


# Process-wide singleton
_logger_instance: Optional[AgentLogger] = None


def get_agent_logger() -> AgentLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = AgentLogger()
    return _logger_instance
