"""Three-Tier Safety and Permission System for AYRA Desktop Agent.

Tiers:
  TIER 1 (Low Risk / Auto):
    Read screen, inspect files, read web pages, open applications, move mouse,
    read system status, read-only terminal commands.
    -> Executes automatically.

  TIER 2 (Medium Risk / Monitored):
    Create files, edit project code, run builds, run tests, type into an editor,
    modify project files.
    -> Executes automatically with notification and structured logging.

  TIER 3 (High Risk / Gated):
    Permanent file deletion, sending emails/messages, purchases/payments,
    changing passwords, security system modification, rebooting the system,
    killing critical system services, destructive shell commands (format, rm -rf /).
    -> Halts execution and requires explicit user confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple


class RiskTier(str, Enum):
    TIER_1_AUTO = "tier_1_auto"
    TIER_2_MONITORED = "tier_2_monitored"
    TIER_3_GATED = "tier_3_gated"


@dataclass(frozen=True)
class SecurityEvaluation:
    tier: RiskTier
    action_description: str
    requires_confirmation: bool
    warning_message: Optional[str] = None


# Known destructive shell patterns
_DESTRUCTIVE_COMMAND_PATTERNS = [
    r"\b(rmdir\s+/s|del\s+/f|format\s+[a-z]:|diskpart|mkfs|dd\s+if=)",
    r"\b(rm\s+-rf\s+(/|~|\*))",
    r"\b(reg\s+delete|shutdown\s+/(r|s))",
    r"\b(drop\s+database|truncate\s+table)",
]

# Sensitive file path patterns that must never be modified or deleted
_PROTECTED_PATH_PATTERNS = [
    r"^[a-zA-Z]:\\(windows|system32|recovery|boot)",
    r"^/etc/(passwd|shadow|sudoers)",
    r"(id_rsa|id_ed25519|\.kdbx|\.pem|\.key|ntuser\.dat|sam|security|system)$",
]


class AgentSecurityManager:
    """Evaluates requested tool actions against the 3-tier security policy."""

    def __init__(self, confirmation_callback: Optional[Callable[[str], bool]] = None) -> None:
        self.confirmation_callback = confirmation_callback

    def evaluate_action(self, tool_name: str, args: Dict[str, Any]) -> SecurityEvaluation:
        """Evaluate the risk tier of a specific action."""
        # 1. Check for explicit destructive file deletion
        if tool_name in ("delete_file", "delete_directory"):
            path = str(args.get("path", ""))
            return SecurityEvaluation(
                tier=RiskTier.TIER_3_GATED,
                action_description=f"Permanently delete '{path}'",
                requires_confirmation=True,
                warning_message=f"Permanent deletion of '{path}' requires explicit user confirmation.",
            )

        # 2. Check for protected path modifications
        target_path = str(args.get("path", "") or args.get("source", "") or args.get("destination", "")).lower()
        for pattern in _PROTECTED_PATH_PATTERNS:
            if re.search(pattern, target_path, re.IGNORECASE):
                return SecurityEvaluation(
                    tier=RiskTier.TIER_3_GATED,
                    action_description=f"Modify protected system path '{target_path}'",
                    requires_confirmation=True,
                    warning_message=f"Modifying system path '{target_path}' is protected and requires confirmation.",
                )

        # 3. Check for terminal commands
        if tool_name in ("run_terminal_command", "terminal_command", "pc_shell"):
            cmd = str(args.get("command", "") or args.get("cmd", "")).lower()
            for pattern in _DESTRUCTIVE_COMMAND_PATTERNS:
                if re.search(pattern, cmd):
                    return SecurityEvaluation(
                        tier=RiskTier.TIER_3_GATED,
                        action_description=f"Execute dangerous command '{cmd}'",
                        requires_confirmation=True,
                        warning_message=f"Command '{cmd}' may be destructive. Confirmation required.",
                    )
            # Normal build/dev/test commands are Tier 2
            if any(term in cmd for term in ("pytest", "npm test", "cargo test", "python", "node", "javac", "git commit", "pip install")):
                return SecurityEvaluation(
                    tier=RiskTier.TIER_2_MONITORED,
                    action_description=f"Run development command '{cmd}'",
                    requires_confirmation=False,
                )
            # Inspection commands are Tier 1
            return SecurityEvaluation(
                tier=RiskTier.TIER_1_AUTO,
                action_description=f"Run command '{cmd}'",
                requires_confirmation=False,
            )

        # 4. File edits and creations
        if tool_name in ("create_file", "write_file", "edit_file", "move_file", "copy_file"):
            path = args.get("path") or args.get("destination")
            return SecurityEvaluation(
                tier=RiskTier.TIER_2_MONITORED,
                action_description=f"Modify or create file '{path}'",
                requires_confirmation=False,
            )

        # 5. Purchases, credentials, messaging
        if tool_name in ("send_email", "make_payment", "change_password", "reboot_system"):
            return SecurityEvaluation(
                tier=RiskTier.TIER_3_GATED,
                action_description=f"Execute high-impact action '{tool_name}'",
                requires_confirmation=True,
                warning_message=f"Action '{tool_name}' requires immediate user confirmation.",
            )

        # 6. Default Tier 1: UI observations, mouse clicks, key presses, app launches, screen reads
        return SecurityEvaluation(
            tier=RiskTier.TIER_1_AUTO,
            action_description=f"Execute '{tool_name}'",
            requires_confirmation=False,
        )

    def is_action_allowed(self, tool_name: str, args: Dict[str, Any], user_confirmed: bool = False) -> Tuple[bool, Optional[str]]:
        """Verify whether an action is permitted to run."""
        eval_result = self.evaluate_action(tool_name, args)
        if eval_result.requires_confirmation:
            if user_confirmed:
                return True, None
            if self.confirmation_callback:
                allowed = self.confirmation_callback(eval_result.action_description)
                if allowed:
                    return True, None
            return False, eval_result.warning_message or f"Action '{tool_name}' requires confirmation."
        return True, None
