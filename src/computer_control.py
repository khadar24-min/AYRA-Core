"""Computer Control Engine for AYRA.

Provides programmatic mouse, keyboard, clipboard, and window management.
Includes emergency failsafe (moving mouse to screen corners aborts action).

Does not rely on hardcoded coordinates for normal operation: coordinates
are fed from screen observation / UI automation locators.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional, Tuple


# Optional PyAutoGUI import with defensive fallback
try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _HAS_PYAUTOGUI = True
except Exception:
    _HAS_PYAUTOGUI = False

# Optional Pyperclip import
try:
    import pyperclip
    _HAS_PYPERCLIP = True
except Exception:
    _HAS_PYPERCLIP = False


class ComputerController:
    """Manages mouse, keyboard, clipboard, and window actions."""

    def __init__(self) -> None:
        self.screen_width = 1920
        self.screen_height = 1080
        if _HAS_PYAUTOGUI:
            try:
                self.screen_width, self.screen_height = pyautogui.size()
            except Exception:
                pass

    # --- Mouse Actions ---

    def mouse_move(self, x: int, y: int, duration: float = 0.2) -> Tuple[bool, str]:
        """Smoothly move mouse pointer to (x, y)."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            pyautogui.moveTo(x, y, duration=duration)
            return True, f"Moved mouse to ({x}, {y})"
        except Exception as exc:
            return False, f"mouse_move failed: {exc}"

    def mouse_click(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left", clicks: int = 1) -> Tuple[bool, str]:
        """Click mouse button at current or specified coordinate."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            if x is not None and y is not None:
                pyautogui.click(x=x, y=y, clicks=clicks, button=button)
                return True, f"Clicked {button} button at ({x}, {y}) (count: {clicks})"
            else:
                pyautogui.click(clicks=clicks, button=button)
                return True, f"Clicked {button} button at current cursor (count: {clicks})"
        except Exception as exc:
            return False, f"mouse_click failed: {exc}"

    def mouse_double_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Tuple[bool, str]:
        """Double click at specified or current coordinates."""
        return self.mouse_click(x=x, y=y, button="left", clicks=2)

    def mouse_right_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Tuple[bool, str]:
        """Right click at specified or current coordinates."""
        return self.mouse_click(x=x, y=y, button="right", clicks=1)

    def mouse_drag(self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.5) -> Tuple[bool, str]:
        """Drag mouse from (start_x, start_y) to (end_x, end_y)."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            pyautogui.moveTo(start_x, start_y)
            pyautogui.dragTo(end_x, end_y, duration=duration, button="left")
            return True, f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y})"
        except Exception as exc:
            return False, f"mouse_drag failed: {exc}"

    def mouse_scroll(self, clicks: int) -> Tuple[bool, str]:
        """Scroll vertical wheel (positive = up, negative = down)."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            pyautogui.scroll(clicks)
            return True, f"Scrolled wheel by {clicks}"
        except Exception as exc:
            return False, f"mouse_scroll failed: {exc}"

    # --- Keyboard Actions ---

    def keyboard_type(self, text: str, interval: float = 0.01) -> Tuple[bool, str]:
        """Type characters sequentially into the currently active window."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            # If text is long or has special unicode, clipboard paste is much faster and cleaner
            if len(text) > 40 or any(ord(c) > 127 for c in text):
                return self.clipboard_paste(text)
            pyautogui.write(text, interval=interval)
            return True, f"Typed text ({len(text)} chars)"
        except Exception as exc:
            return False, f"keyboard_type failed: {exc}"

    def key_press(self, key: str) -> Tuple[bool, str]:
        """Press and release a single key (e.g. 'enter', 'esc', 'tab', 'backspace')."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            pyautogui.press(key)
            return True, f"Pressed key '{key}'"
        except Exception as exc:
            return False, f"key_press failed: {exc}"

    def hotkey(self, *keys: str) -> Tuple[bool, str]:
        """Press and release a shortcut combination (e.g. 'ctrl', 's' or 'alt', 'tab')."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            pyautogui.hotkey(*keys)
            return True, f"Triggered hotkey {list(keys)}"
        except Exception as exc:
            return False, f"hotkey failed: {exc}"

    def clipboard_paste(self, text: str) -> Tuple[bool, str]:
        """Set clipboard text and trigger Ctrl+V paste."""
        if not _HAS_PYAUTOGUI:
            return False, "pyautogui library not available."
        try:
            if _HAS_PYPERCLIP:
                pyperclip.copy(text)
                time.sleep(0.05)
                pyautogui.hotkey("ctrl", "v")
                return True, f"Pasted text via clipboard ({len(text)} chars)"
            else:
                pyautogui.write(text, interval=0.01)
                return True, f"Typed text ({len(text)} chars)"
        except Exception as exc:
            return False, f"clipboard_paste failed: {exc}"

    # --- Window Management ---

    def list_windows(self) -> List[Dict[str, Any]]:
        """List currently visible and titled application windows."""
        windows: List[Dict[str, Any]] = []
        if sys.platform == "win32":
            try:
                import ctypes
                EnumWindows = ctypes.windll.user32.EnumWindows
                EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
                GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
                GetWindowText = ctypes.windll.user32.GetWindowTextW
                IsWindowVisible = ctypes.windll.user32.IsWindowVisible

                def foreach_window(hwnd, lParam):
                    if IsWindowVisible(hwnd):
                        length = GetWindowTextLength(hwnd)
                        if length > 0:
                            buff = ctypes.create_unicode_buffer(length + 1)
                            GetWindowText(hwnd, buff, length + 1)
                            title = buff.value.strip()
                            if title and title != "Program Manager":
                                windows.append({"hwnd": hwnd, "title": title})
                    return True

                EnumWindows(EnumWindowsProc(foreach_window), 0)
            except Exception:
                pass
        return windows

    def focus_window(self, title_pattern: str) -> Tuple[bool, str]:
        """Bring the window matching title_pattern to the foreground."""
        pattern_lower = title_pattern.lower()
        if sys.platform == "win32":
            try:
                import ctypes
                windows = self.list_windows()
                for win in windows:
                    if pattern_lower in win["title"].lower():
                        hwnd = win["hwnd"]
                        # Restore if minimized
                        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        return True, f"Focused window: '{win['title']}'"
                return False, f"No window matching '{title_pattern}' found."
            except Exception as exc:
                return False, f"focus_window error: {exc}"
        return False, "Window focus not supported on this platform."

    def maximize_window(self, title_pattern: str) -> Tuple[bool, str]:
        """Maximize the window matching title_pattern."""
        pattern_lower = title_pattern.lower()
        if sys.platform == "win32":
            try:
                import ctypes
                windows = self.list_windows()
                for win in windows:
                    if pattern_lower in win["title"].lower():
                        hwnd = win["hwnd"]
                        ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        return True, f"Maximized window: '{win['title']}'"
                return False, f"No window matching '{title_pattern}' found."
            except Exception as exc:
                return False, f"maximize_window error: {exc}"
        return False, "Window maximize not supported on this platform."

    def minimize_window(self, title_pattern: str) -> Tuple[bool, str]:
        """Minimize the window matching title_pattern."""
        pattern_lower = title_pattern.lower()
        if sys.platform == "win32":
            try:
                import ctypes
                windows = self.list_windows()
                for win in windows:
                    if pattern_lower in win["title"].lower():
                        hwnd = win["hwnd"]
                        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                        return True, f"Minimized window: '{win['title']}'"
                return False, f"No window matching '{title_pattern}' found."
            except Exception as exc:
                return False, f"minimize_window error: {exc}"
        return False, "Window minimize not supported on this platform."


# Process-wide controller singleton
_controller_instance: Optional[ComputerController] = None


def get_computer_controller() -> ComputerController:
    global _controller_instance
    if _controller_instance is None:
        _controller_instance = ComputerController()
    return _controller_instance
