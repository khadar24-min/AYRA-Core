"""Autonomous Agent Execution Loop for AYRA.

Implements the continuous execution loop:
  OBSERVE
  -> PLAN
  -> SELECT CAPABILITIES
  -> ACT
  -> OBSERVE RESULT
  -> VERIFY
  -> RECOVER / REPLAN
  -> CONTINUE
  -> COMPLETE

Provides concise progress callbacks suitable for spoken TTS updates:
  "Opening Chrome."
  "CodeTantra is open."
  "I found the editor."
  "Fixed the error and verified the output."
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from src.agent_logger import get_agent_logger
from src.agent_planner import PlanStatus, StepStatus, TaskPlan, TaskStep, get_task_planner
from src.agent_security import AgentSecurityManager
from src.app_discovery import get_app_discovery
from src.browser_agent import get_browser_agent
from src.computer_control import get_computer_controller
from src.file_agent import get_file_agent
from src.screen_agent import get_screen_agent
from src.terminal_agent import get_terminal_agent


class AutonomousAgentLoop:
    """Orchestrates multi-step computer tasks to completion."""

    def __init__(
        self,
        security_manager: Optional[AgentSecurityManager] = None,
        on_milestone_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.planner = get_task_planner()
        self.discovery = get_app_discovery()
        self.computer = get_computer_controller()
        self.screen = get_screen_agent()
        self.browser = get_browser_agent()
        self.files = get_file_agent()
        self.terminal = get_terminal_agent()
        self.logger = get_agent_logger()
        self.security = security_manager or AgentSecurityManager()
        self.on_milestone = on_milestone_callback
        self.active_plan: Optional[TaskPlan] = None

    def _notify(self, message: str) -> None:
        """Surface a concise spoken/UI milestone update."""
        if self.on_milestone:
            try:
                self.on_milestone(message)
            except Exception:
                pass

    def execute_action(self, step: TaskStep) -> Tuple[bool, str]:
        """Execute a single step action across local subsystem agents."""
        action = step.action
        args = step.args

        # Safety Gate Check
        allowed, reason = self.security.is_action_allowed(action, args, user_confirmed=args.get("confirmed", False))
        if not allowed:
            return False, f"Action blocked by safety policy: {reason}"

        # 1. Computer Control & App Discovery
        if action == "launch_app":
            app_name = args.get("app_name", "")
            return self.discovery.launch_application(app_name, args.get("args"))
        elif action == "focus_window":
            return self.computer.focus_window(args.get("title", ""))
        elif action == "mouse_click":
            return self.computer.mouse_click(args.get("x"), args.get("y"))
        elif action == "keyboard_type":
            return self.computer.keyboard_type(args.get("text", ""))

        # 2. Browser Actions
        elif action == "open_url":
            return self.browser.open_url(args.get("url", ""))
        elif action == "click_element":
            return self.browser.click_element_by_text(args.get("label", ""))
        elif action == "type_text":
            return self.browser.type_into_input(args.get("text", ""), args.get("label"))
        elif action == "inspect_page":
            summary = self.browser.read_page_summary()
            return True, summary

        # 3. Screen Actions
        elif action == "observe_screen":
            img = self.screen.capture_screenshot()
            if img:
                return True, f"Observed screen ({img.size[0]}x{img.size[1]})"
            return False, "Failed to capture display"
        elif action == "find_element":
            match = self.screen.find_element(args.get("label", ""))
            if match:
                return True, f"Found '{match.text}' at ({match.center_x}, {match.center_y})"
            return False, f"Could not locate element '{args.get('label')}' on screen"

        # 4. File Actions
        elif action == "create_file":
            return self.files.create_file(args.get("path", ""), args.get("content", ""))
        elif action == "read_file":
            return self.files.read_file(args.get("path", ""))
        elif action == "edit_file":
            return self.files.edit_file(args.get("path", ""), args.get("target", ""), args.get("replacement", ""))
        elif action == "search_files":
            results = self.files.search_files(args.get("base_dir", "."), args.get("pattern", "*"))
            summary = f"Found {len(results)} matching files."
            if results:
                summary += f" First match: {results[0]['path']}"
            return True, summary
        elif action == "move_file":
            return self.files.move_file(args.get("source", ""), args.get("destination", ""))
        elif action == "copy_file":
            return self.files.copy_file(args.get("source", ""), args.get("destination", ""))
        elif action == "delete_file":
            return self.files.delete_file(args.get("path", ""), args.get("confirmed", False))

        # 5. Terminal Actions
        elif action == "run_command":
            res = self.terminal.execute_command(
                args.get("command", ""),
                cwd=args.get("cwd"),
                timeout_sec=args.get("timeout_sec", 30.0),
            )
            if res.exit_code == 0:
                out = res.stdout if res.stdout else "Executed cleanly with no output."
                return True, out
            return False, f"Process exited with code {res.exit_code}. Error: {res.stderr or res.stdout}"

        # 6. Fallback
        return True, f"Completed step: {step.description}"

    def verify_action(self, step: TaskStep, success: bool, output: str) -> bool:
        """Verify whether an action achieved its intended outcome."""
        if not success:
            return False

        # If a file creation was requested, verify existence on disk
        if step.action == "create_file":
            p = self.files.resolve_path(step.args.get("path", ""))
            return p.exists()

        # If a file move was requested, verify destination
        if step.action == "move_file":
            p = self.files.resolve_path(step.args.get("destination", ""))
            return p.exists()

        # If command was run, verify non-error output
        if step.action == "run_command":
            return "error:" not in output.lower() and "failed" not in output.lower()

        return True

    def run_task(self, goal: str) -> Generator[str, None, str]:
        """Run the complete autonomous agent loop for the given goal.

        Yields spoken/text progress milestone messages as each step executes,
        and returns the final result summary upon completion.
        """
        plan = self.planner.create_plan(goal)
        self.active_plan = plan
        plan.status = PlanStatus.IN_PROGRESS

        # Initial announcement
        first_step = plan.get_current_step()
        if first_step:
            yield f"Starting task: {first_step.description}."

        step_counter = 0
        while not plan.is_complete() and step_counter < plan.max_steps:
            step_counter += 1
            current_step = plan.get_current_step()
            if not current_step:
                break

            current_step.status = StepStatus.RUNNING

            # ACT
            success, output = self.execute_action(current_step)

            # VERIFY
            verified = self.verify_action(current_step, success, output)

            # Structured logging
            self.logger.log_run_event(
                goal=goal,
                task_id=plan.task_id,
                step_id=current_step.id,
                domain=current_step.domain,
                tool=current_step.action,
                args=current_step.args,
                result=output,
                verified=verified,
                error=None if verified else output,
                final_status="in_progress",
            )

            if verified:
                plan.mark_step_completed(current_step.id, output)
                next_step = plan.get_current_step()
                if next_step:
                    yield f"Completed: {current_step.description}. Next: {next_step.description}."
            else:
                # RECOVER & REPLAN
                has_retry = plan.mark_step_failed(current_step.id, output)
                if has_retry:
                    yield f"Encountered an issue with {current_step.description}. Retrying with adjustment."
                    self.planner.replan_step(plan, current_step, output)
                else:
                    yield f"Unable to complete {current_step.description}. Stopping safely."
                    break

        if plan.status == PlanStatus.COMPLETED:
            final_msg = f"Task completed successfully: {goal}."
        else:
            final_msg = f"Task concluded. {plan.summary()}."

        self.logger.log_run_event(
            goal=goal,
            task_id=plan.task_id,
            step_id=step_counter,
            domain="core",
            tool="complete",
            args={},
            result=final_msg,
            verified=(plan.status == PlanStatus.COMPLETED),
            final_status=plan.status.value,
        )

        self.active_plan = None
        yield final_msg
        return final_msg


# Process-wide loop instance
_loop_instance: Optional[AutonomousAgentLoop] = None


def get_agent_loop() -> AutonomousAgentLoop:
    global _loop_instance
    if _loop_instance is None:
        _loop_instance = AutonomousAgentLoop()
    return _loop_instance
