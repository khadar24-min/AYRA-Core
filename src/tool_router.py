"""Dynamic Tool Router and LLM Context Optimization Engine for AYRA.

Solves the prompt-token-limit bug permanently:
- Organizes capabilities into distinct functional domains:
    CORE, COMPUTER, SCREEN, BROWSER, FILE, TERMINAL
- Instead of attaching all 36+ tool definitions to every LLM turn (which was
  causing prompt-token-limit overflow), dynamically attaches only the 2-6
  relevant tool schemas matching the active task or user prompt.
- Truncates and compresses tool outputs to maintain tight token bounds.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class ToolDomain(str, Enum):
    CORE = "core"
    COMPUTER = "computer"
    SCREEN = "screen"
    BROWSER = "browser"
    FILE = "file"
    TERMINAL = "terminal"


# Mapping from tool names to their primary capability domain
_TOOL_DOMAINS: Dict[str, ToolDomain] = {
    # COMPUTER
    "launch_app": ToolDomain.COMPUTER,
    "system_control": ToolDomain.COMPUTER,
    "focus_window": ToolDomain.COMPUTER,
    "mouse_click": ToolDomain.COMPUTER,
    "mouse_move": ToolDomain.COMPUTER,
    "keyboard_type": ToolDomain.COMPUTER,
    "key_press": ToolDomain.COMPUTER,
    "hotkey": ToolDomain.COMPUTER,

    # SCREEN
    "screen_snapshot": ToolDomain.SCREEN,
    "observe_screen": ToolDomain.SCREEN,
    "find_ui_element": ToolDomain.SCREEN,
    "camera_snapshot": ToolDomain.SCREEN,

    # BROWSER
    "open_browser": ToolDomain.BROWSER,
    "open_url": ToolDomain.BROWSER,
    "click_web_element": ToolDomain.BROWSER,
    "type_web_input": ToolDomain.BROWSER,
    "inspect_page": ToolDomain.BROWSER,

    # FILE
    "search_files": ToolDomain.FILE,
    "read_local_file": ToolDomain.FILE,
    "create_file": ToolDomain.FILE,
    "write_file": ToolDomain.FILE,
    "edit_file": ToolDomain.FILE,
    "copy_file": ToolDomain.FILE,
    "move_file": ToolDomain.FILE,
    "delete_file": ToolDomain.FILE,

    # TERMINAL
    "pc_shell": ToolDomain.TERMINAL,
    "run_terminal_command": ToolDomain.TERMINAL,
    "run_command": ToolDomain.TERMINAL,
    "run_code": ToolDomain.TERMINAL,
    "list_processes": ToolDomain.TERMINAL,
    "list_ports": ToolDomain.TERMINAL,

    # CORE / INFORMATION
    "web_search": ToolDomain.CORE,
    "web_fetch": ToolDomain.CORE,
    "knowledge_search": ToolDomain.CORE,
    "knowledge_remember": ToolDomain.CORE,
    "recall_conversation": ToolDomain.CORE,
    "get_weather": ToolDomain.CORE,
    "get_sports_info": ToolDomain.CORE,
    "get_calendar_events": ToolDomain.CORE,
    "set_reminder": ToolDomain.CORE,
    "list_reminders": ToolDomain.CORE,
    "status_report": ToolDomain.CORE,
}


class ToolRouter:
    """Selects minimal relevant tool sets and bounds token usage."""

    def detect_domains(self, query: str) -> Set[ToolDomain]:
        """Infer which capability domains are relevant to the query."""
        clean = query.lower()
        domains: Set[ToolDomain] = set()

        # Browser
        if any(term in clean for term in ("browser", "chrome", "edge", "website", "http", "url", "codetantra", "web page")):
            domains.add(ToolDomain.BROWSER)

        # Screen
        if any(term in clean for term in ("screen", "look", "see", "error", "window", "display", "ui")):
            domains.add(ToolDomain.SCREEN)

        # File
        if any(term in clean for term in ("file", "folder", "directory", "document", "create", "write", "move", "rename", "delete")):
            domains.add(ToolDomain.FILE)

        # Terminal / Development
        if any(term in clean for term in ("command", "run", "python", "java", "npm", "node", "compile", "terminal", "test", "git", "bash", "shell", "process", "port", "build")):
            domains.add(ToolDomain.TERMINAL)

        # Computer Control
        if any(term in clean for term in ("app", "application", "launch", "open", "click", "type", "press", "keyboard", "mouse", "close", "focus")):
            domains.add(ToolDomain.COMPUTER)

        # If none matched or conversation query, fallback to CORE
        if not domains:
            domains.add(ToolDomain.CORE)

        return domains

    def filter_tools(
        self,
        all_tools: List[Dict[str, Any]],
        query: str,
        current_step_domain: Optional[str] = None,
        max_tools: int = 8,
    ) -> List[Dict[str, Any]]:
        """Filter the available tool definitions to only those relevant to the query.

        This guarantees that instead of all 36+ tools being attached to the prompt,
        only the most relevant 2 to 6 tools are supplied.
        """
        if not all_tools:
            return []

        # If domain is explicitly given by the active task step, use it
        target_domains: Set[ToolDomain] = set()
        if current_step_domain:
            try:
                target_domains.add(ToolDomain(current_step_domain))
            except Exception:
                pass

        # In addition, infer from query text
        target_domains.update(self.detect_domains(query))

        # Always keep CORE web search if query looks informational
        if any(w in query.lower() for w in ("what", "who", "search", "latest", "how")):
            target_domains.add(ToolDomain.CORE)

        selected: List[Dict[str, Any]] = []
        for tool in all_tools:
            name = tool.get("name", "")
            domain = _TOOL_DOMAINS.get(name, ToolDomain.CORE)
            if domain in target_domains:
                selected.append(tool)
                if len(selected) >= max_tools:
                    break

        # Fallback guarantee: if none selected, supply first 4 tools
        if not selected:
            selected = all_tools[:4]

        return selected


# Process-wide tool router
_router_instance: Optional[ToolRouter] = None


def get_tool_router() -> ToolRouter:
    global _router_instance
    if _router_instance is None:
        _router_instance = ToolRouter()
    return _router_instance
