"""Screen Observation & Visual UI Locator for AYRA.

Provides actionable visual grounding:
- Captures primary display screenshots
- Performs visual text and element detection (OCR / Windows UI Automation / PIL)
- Finds UI elements by label, placeholder, or text content
- Returns exact screen coordinates (center x, y) and bounding boxes (x, y, w, h)
- Bridges the gap between high-level instructions and physical UI clicks
"""

from __future__ import annotations

import io
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageGrab
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


@dataclass
class UIElementMatch:
    text: str
    center_x: int
    center_y: int
    bbox: Tuple[int, int, int, int]  # (x, y, width, height)
    confidence: float
    element_type: str = "generic"


class ScreenAgent:
    """Observes the display and resolves UI element coordinates."""

    def __init__(self) -> None:
        self.last_screenshot: Optional[Any] = None

    def capture_screenshot(self) -> Optional[Any]:
        """Capture the current screen into a PIL Image."""
        if not _HAS_PIL:
            return None
        try:
            img = ImageGrab.grab()
            self.last_screenshot = img
            return img
        except Exception as exc:
            print(f"[screen_agent] capture error: {exc}", file=sys.stderr)
            return None

    def capture_screenshot_bytes(self) -> Optional[bytes]:
        """Capture and encode screenshot as PNG bytes."""
        img = self.capture_screenshot()
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def get_screen_dimensions(self) -> Tuple[int, int]:
        """Return (width, height) of the primary display."""
        if self.last_screenshot:
            return self.last_screenshot.size
        if _HAS_PIL:
            try:
                img = ImageGrab.grab()
                self.last_screenshot = img
                return img.size
            except Exception:
                pass
        return (1920, 1080)

    def _find_via_ui_automation(self, label: str) -> Optional[UIElementMatch]:
        """Query Windows UI Automation for controls matching the label."""
        if sys.platform != "win32":
            return None
        try:
            import uiautomation as auto  # type: ignore
            clean = label.strip().lower()
            root = auto.GetRootControl()
            # Search children for matching Name
            for ctrl in root.GetChildren():
                name = getattr(ctrl, "Name", "").lower()
                if clean in name:
                    rect = ctrl.BoundingRectangle
                    if rect.width() > 0 and rect.height() > 0:
                        cx = rect.left + rect.width() // 2
                        cy = rect.top + rect.height() // 2
                        return UIElementMatch(
                            text=ctrl.Name,
                            center_x=cx,
                            center_y=cy,
                            bbox=(rect.left, rect.top, rect.width(), rect.height()),
                            confidence=0.95,
                            element_type=getattr(ctrl, "ControlTypeName", "Control"),
                        )
        except Exception:
            pass
        return None

    def _find_via_ocr(self, label: str, image: Any) -> Optional[UIElementMatch]:
        """Query pytesseract OCR for matching bounding boxes if installed."""
        try:
            import pytesseract
            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            clean = label.strip().lower()
            num_boxes = len(data["text"])
            for i in range(num_boxes):
                word = str(data["text"][i]).strip().lower()
                if clean in word or (word and word in clean):
                    x = int(data["left"][i])
                    y = int(data["top"][i])
                    w = int(data["width"][i])
                    h = int(data["height"][i])
                    return UIElementMatch(
                        text=data["text"][i],
                        center_x=x + w // 2,
                        center_y=y + h // 2,
                        bbox=(x, y, w, h),
                        confidence=float(data["conf"][i]) / 100.0,
                        element_type="text",
                    )
        except Exception:
            pass
        return None

    def find_element(self, label_or_text: str) -> Optional[UIElementMatch]:
        """Locate the screen coordinates of a named UI element or text."""
        # 1. Try Windows UI Automation first (semantic and instant)
        match = self._find_via_ui_automation(label_or_text)
        if match:
            return match

        # 2. Capture fresh frame
        img = self.capture_screenshot()
        if img:
            match = self._find_via_ocr(label_or_text, img)
            if match:
                return match

        # 3. Fallback for common standard positions if requested by description
        clean = label_or_text.lower().strip()
        width, height = self.get_screen_dimensions()

        if "start" in clean or "windows logo" in clean:
            return UIElementMatch("Start Button", 24, height - 24, (0, height - 48, 48, 48), 0.8)
        elif "center" in clean:
            return UIElementMatch("Screen Center", width // 2, height // 2, (0, 0, width, height), 0.7)
        elif "top right" in clean or "close" in clean:
            return UIElementMatch("Top Right Close", width - 20, 15, (width - 40, 0, 40, 30), 0.7)

        return None


# Process-wide screen agent
_screen_agent_instance: Optional[ScreenAgent] = None


def get_screen_agent() -> ScreenAgent:
    global _screen_agent_instance
    if _screen_agent_instance is None:
        _screen_agent_instance = ScreenAgent()
    return _screen_agent_instance
