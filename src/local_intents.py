"""Local Intent Fast-Path for AYRA.

Handles simple deterministic requests locally with zero LLM API calls and zero tokens:
- Time and date requests (grounded in system/Windows clock)
- Identity requests (AYRA, created by Khadar / Nannu)
- Basic system status (battery, CPU, RAM)
- Active task status queries ("What are you doing right now?")

Returns a string response when an intent matches, or None when the request should
proceed to the task planner or conversational LLM.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Optional


def _get_windows_time() -> str:
    """Get authoritative local time from Windows clock (even when under WSL)."""
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "time", "/t"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        windows_time = result.stdout.strip()
        if windows_time:
            return windows_time.lstrip("0")
    except Exception:
        pass
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def _get_local_date() -> str:
    """Format current date naturally."""
    return datetime.now().strftime("%A, %B %d, %Y")


def _get_system_snapshot() -> str:
    """Get battery, CPU, and RAM snapshot via psutil if available."""
    lines = []
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        lines.append(f"CPU usage is at {cpu:.0f}%, and RAM is at {mem.percent:.0f}%.")
        
        battery = psutil.sensors_battery()
        if battery is not None:
            status = "charging" if battery.power_plugged else "on battery"
            lines.append(f"Battery is at {battery.percent:.0f}% ({status}).")
    except Exception:
        lines.append("System status: core services are active and running normally.")
    
    return " ".join(lines)


# Identity and Creator facts
AYRA_NAME = "AYRA"
AYRA_CREATOR = "Khadar, also known as Nannu"
AYRA_PURPOSE = "an autonomous desktop AI assistant"
AYRA_WAKE_WORD = "Hey AYRA"


# Fast regex patterns
_TIME_PATTERNS = [
    r"^(what('s|\s+is)\s+the\s+time(\s+now)?|what\s+time\s+is\s+it(\s+now)?|tell\s+me\s+the\s+time|current\s+time|time\s+check)[\?\.\!]?$",
    r"^(what|tell|give|show)\s+(me\s+)?(the\s+)?(current\s+)?(time|clock)[\?\.\!]?$",
]

_DATE_PATTERNS = [
    r"^(what('s|\s+is)\s+(the\s+)?(today('s)?\s+)?date(\s+today)?|what\s+day\s+is\s+it(\s+today)?|what('s|\s+is)\s+today('s\s+date)?)[\?\.\!]?$",
    r"^(tell\s+me\s+)?(the\s+)?(date|day)(\s+today)?[\?\.\!]?$",
]

_IDENTITY_WHO_PATTERNS = [
    r"^(who\s+are\s+you|what\s+is\s+your\s+name|what\s+are\s+you|tell\s+me\s+about\s+yourself)[\?\.\!]?$",
]

_IDENTITY_CREATOR_PATTERNS = [
    r"^(who\s+(created|made|built|programmed|developed)\s+you|who\s+is\s+your\s+(creator|maker|developer)|who('s|\s+is)\s+your\s+boss)[\?\.\!]?$",
]

_SYSTEM_STATUS_PATTERNS = [
    r"^(how('s|\s+is)\s+(the\s+)?(system|pc|laptop|computer)(\s+doing)?|system\s+status|battery\s+status|what('s|\s+is)\s+the\s+battery(\s+level)?)[\?\.\!]?$",
]

_ACTIVE_TASK_PATTERNS = [
    r"^(what\s+are\s+you\s+doing(\s+now|\s+right\s+now)?|what\s+step\s+are\s+you\s+on|what('s|\s+is)\s+your\s+current\s+task|task\s+status)[\?\.\!]?$",
]


def check_local_intent(text: str, active_task_summary: Optional[str] = None) -> Optional[str]:
    """Check if the user request matches a deterministic local intent.

    Returns the formatted string response if handled, or None if it needs
    agent planning or full LLM generation.
    """
    clean = text.strip().lower()
    clean = re.sub(r"^(hey\s+ayra|ayra|hey\s+jarvis|jarvis)[,\s]+", "", clean).strip()

    # 1. Time
    for pattern in _TIME_PATTERNS:
        if re.search(pattern, clean):
            now_time = _get_windows_time()
            return f"It is {now_time}."

    # 2. Date
    for pattern in _DATE_PATTERNS:
        if re.search(pattern, clean):
            now_date = _get_local_date()
            return f"Today is {now_date}."

    # 3. Creator
    for pattern in _IDENTITY_CREATOR_PATTERNS:
        if re.search(pattern, clean):
            return f"I was created by {AYRA_CREATOR}."

    # 4. Identity
    for pattern in _IDENTITY_WHO_PATTERNS:
        if re.search(pattern, clean):
            return (
                f"I am {AYRA_NAME}, {AYRA_PURPOSE} created by {AYRA_CREATOR}. "
                f"You can speak to me by saying '{AYRA_WAKE_WORD}' or typing your request."
            )

    # 5. Active Task Status
    for pattern in _ACTIVE_TASK_PATTERNS:
        if re.search(pattern, clean):
            if active_task_summary:
                return active_task_summary
            return "I am currently idle and ready for your command."

    # 6. Basic System Status
    for pattern in _SYSTEM_STATUS_PATTERNS:
        if re.search(pattern, clean):
            return _get_system_snapshot()

    return None
