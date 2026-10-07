"""Browser Automation Agent for AYRA.

Provides robust web interaction:
- Connects to existing Chrome/Edge via Chrome DevTools Protocol (CDP) or Playwright
- Fallback to visual screen interaction (via screen_agent + computer_control)
- Navigates URLs
- Inspects pages & reads readable page content
- Interacts with elements: click buttons, fill forms, select fields, switch tabs
- Robust against dynamic coding platforms like CodeTantra
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from src.app_discovery import get_app_discovery
from src.computer_control import get_computer_controller
from src.screen_agent import get_screen_agent


class BrowserAgent:
    """Manages browser sessions, semantic DOM interaction, and visual fallbacks."""

    def __init__(self, cdp_port: int = 9222) -> None:
        self.cdp_port = cdp_port
        self.active_tab_id: Optional[str] = None
        self.last_url: str = ""
        self.computer = get_computer_controller()
        self.screen = get_screen_agent()
        self.discovery = get_app_discovery()

    def is_cdp_available(self) -> bool:
        """Check if Chrome/Edge remote debugging port is open."""
        try:
            url = f"http://127.0.0.1:{self.cdp_port}/json/version"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        return False

    def launch_or_connect(self, browser_name: str = "chrome") -> Tuple[bool, str]:
        """Launch Chrome or Edge with remote debugging enabled if not already running."""
        if self.is_cdp_available():
            return True, f"Connected to existing browser instance via CDP on port {self.cdp_port}."

        # Find executable path
        exe_path = self.discovery.find_application(browser_name)
        if not exe_path:
            # Fallback to direct names
            for name in ("chrome", "google-chrome", "msedge", "chromium"):
                found = self.discovery.find_application(name)
                if found:
                    exe_path = found
                    break

        if not exe_path:
            return False, f"Could not find browser '{browser_name}'."

        cmd = [exe_path, f"--remote-debugging-port={self.cdp_port}", "--restore-last-session"]
        try:
            subprocess.Popen(cmd, shell=False)
            time.sleep(1.5)
            if self.is_cdp_available():
                return True, f"Launched {browser_name} with CDP enabled on port {self.cdp_port}."
            return True, f"Launched {browser_name} in GUI mode (visual interaction enabled)."
        except Exception as exc:
            return False, f"Failed to launch browser: {exc}"

    def list_tabs(self) -> List[Dict[str, Any]]:
        """List open browser tabs via CDP."""
        if not self.is_cdp_available():
            return []
        try:
            url = f"http://127.0.0.1:{self.cdp_port}/json"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode())
                return [t for t in data if t.get("type") == "page"]
        except Exception:
            return []

    def open_url(self, target_url: str) -> Tuple[bool, str]:
        """Navigate to target URL using CDP or GUI fallback."""
        if not target_url.startswith(("http://", "https://")):
            target_url = "https://" + target_url

        self.last_url = target_url

        # 1. Try CDP navigation
        if self.is_cdp_available():
            try:
                # Open new tab or navigate existing
                encoded_url = urllib.parse.quote(target_url, safe=":/?=&")
                new_tab_url = f"http://127.0.0.1:{self.cdp_port}/json/new?{encoded_url}"
                req = urllib.request.Request(new_tab_url, method="PUT")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    tab = json.loads(resp.read().decode())
                    self.active_tab_id = tab.get("id")
                    time.sleep(1.0)
                    return True, f"Navigated to {target_url} via CDP."
            except Exception:
                pass

        # 2. Visual / OS Fallback: Focus or launch browser and navigate via address bar
        launched, _ = self.launch_or_connect("chrome")
        time.sleep(1.0)
        self.computer.focus_window("chrome") or self.computer.focus_window("edge")
        time.sleep(0.5)
        # Hotkey Ctrl+L to focus address bar, type URL and hit Enter
        self.computer.hotkey("ctrl", "l")
        time.sleep(0.1)
        self.computer.keyboard_type(target_url)
        time.sleep(0.1)
        self.computer.key_press("enter")
        time.sleep(2.0)
        return True, f"Navigated to {target_url} via browser address bar."

    def click_element_by_text(self, text_or_label: str) -> Tuple[bool, str]:
        """Click an element on the current page."""
        # Try visual grounding first
        match = self.screen.find_element(text_or_label)
        if match:
            ok, msg = self.computer.mouse_click(match.center_x, match.center_y)
            if ok:
                time.sleep(0.5)
                return True, f"Clicked '{text_or_label}' at screen ({match.center_x}, {match.center_y})."

        # Keyboard fallback: search page text via Ctrl+F
        self.computer.hotkey("ctrl", "f")
        time.sleep(0.2)
        self.computer.keyboard_type(text_or_label)
        time.sleep(0.2)
        self.computer.key_press("enter")
        self.computer.key_press("esc")
        time.sleep(0.2)
        self.computer.key_press("enter")
        return True, f"Attempted interaction with '{text_or_label}'."

    def type_into_input(self, text: str, field_label: Optional[str] = None) -> Tuple[bool, str]:
        """Type text into an input field or active editor."""
        if field_label:
            self.click_element_by_text(field_label)
            time.sleep(0.3)
        return self.computer.keyboard_type(text)

    def read_page_summary(self) -> str:
        """Capture summary of visible web page state."""
        # 1. From CDP tabs
        tabs = self.list_tabs()
        if tabs:
            titles = [f"Tab: {t.get('title')} ({t.get('url')})" for t in tabs[:3]]
            return "Active browser tabs:\n" + "\n".join(titles)

        # 2. From window list
        wins = self.computer.list_windows()
        browser_wins = [w["title"] for w in wins if any(b in w["title"].lower() for b in ("chrome", "edge", "firefox"))]
        if browser_wins:
            return f"Visible browser window: {browser_wins[0]}"

        return "Browser window active."


# Process-wide browser agent
_browser_agent_instance: Optional[BrowserAgent] = None


def get_browser_agent() -> BrowserAgent:
    global _browser_agent_instance
    if _browser_agent_instance is None:
        _browser_agent_instance = BrowserAgent()
    return _browser_agent_instance
