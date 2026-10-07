"""Task Understanding and Hierarchical Planner for AYRA.

Converts arbitrary high-level user instructions into structured, executable plans:
- Goal decomposition into discrete TaskSteps
- Domain and capability mapping
- Verification criteria
- Plan state tracking (current, completed, failed, retried)
- Dynamic replanning upon failure
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskStep:
    id: int
    description: str
    domain: str  # "computer", "screen", "browser", "file", "terminal", "core"
    action: str  # e.g. "launch_app", "navigate_url", "create_file", "run_command"
    args: Dict[str, Any] = field(default_factory=dict)
    verification: str = ""
    status: StepStatus = StepStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    max_retries: int = 2


@dataclass
class TaskPlan:
    task_id: str
    goal: str
    steps: List[TaskStep]
    status: PlanStatus = PlanStatus.NOT_STARTED
    current_step_index: int = 0
    max_steps: int = 15

    def get_current_step(self) -> Optional[TaskStep]:
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    def mark_step_completed(self, step_id: int, result: str) -> None:
        for idx, step in enumerate(self.steps):
            if step.id == step_id:
                step.status = StepStatus.COMPLETED
                step.result = result
                self.current_step_index = idx + 1
                break
        if self.current_step_index >= len(self.steps):
            self.status = PlanStatus.COMPLETED

    def mark_step_failed(self, step_id: int, error: str) -> bool:
        """Mark step failed; returns True if retries are available."""
        for step in self.steps:
            if step.id == step_id:
                step.error = error
                if step.retries < step.max_retries:
                    step.retries += 1
                    step.status = StepStatus.PENDING
                    return True
                else:
                    step.status = StepStatus.FAILED
                    self.status = PlanStatus.FAILED
                    return False
        return False

    def is_complete(self) -> bool:
        return self.status == PlanStatus.COMPLETED or self.current_step_index >= len(self.steps)

    def summary(self) -> str:
        done = sum(1 for s in self.steps if s.status == StepStatus.COMPLETED)
        total = len(self.steps)
        curr = self.get_current_step()
        curr_desc = curr.description if curr else "None"
        return f"Task '{self.goal}' [{done}/{total} steps completed]. Current step: {curr_desc} (Status: {self.status.value})"


def is_computer_task(text: str) -> bool:
    """Classify whether user utterance represents an autonomous computer task."""
    clean = text.lower().strip()
    # Check for direct computer control action verbs
    patterns = [
        r"\b(open|launch|start|run|close)\s+(chrome|edge|vs\s*code|vscode|spotify|calculator|notepad|terminal|browser|codetantra|app|application)\b",
        r"\b(create|write|make|generate)\s+a?\s*(python|java|js|text|c\+\+|html)?\s*(file|program|script|document|code)\b",
        r"\b(run|execute|test|compile|debug)\s+(this|the|my)?\s*(code|program|script|test|project)\b",
        r"\b(find|search|move|copy|rename|delete)\s+(this|the|a)?\s*(file|folder|directory)\b",
        r"\b(look\s+at\s+(my|the)?\s*screen|what('s|\s+is)\s+on\s+my\s+screen|fix\s+(this|the)?\s*(error|project|bug))\b",
        r"\b(navigate\s+to|open\s+(this\s+)?website|fill\s+out|type\s+into)\b",
    ]
    return any(re.search(p, clean) for p in patterns)


class TaskPlanner:
    """Decomposes user goals into structured TaskPlans."""

    def create_plan(self, goal: str) -> TaskPlan:
        """Create an initial task plan from natural language goal."""
        task_id = str(uuid.uuid4())[:8]
        clean = goal.lower().strip()
        steps: List[TaskStep] = []

        # 1. Pattern: "Open Chrome" / "Open Edge" / "Open VS Code"
        match_open = re.search(r"^(open|launch|start)\s+([a-zA-Z0-9_\-\.\s]+)$", clean)
        if match_open and not any(k in clean for k in ("and", "write", "run", "fix", "file")):
            app_name = match_open.group(2).strip()
            steps.append(TaskStep(
                id=1,
                description=f"Launch application '{app_name}'",
                domain="computer",
                action="launch_app",
                args={"app_name": app_name},
                verification=f"Verify window for '{app_name}' is active",
            ))

        # 2. Pattern: "Open VS Code and open my AYRA project"
        elif "vs code" in clean and "ayra" in clean:
            steps.append(TaskStep(
                id=1,
                description="Launch VS Code with project directory",
                domain="terminal",
                action="run_command",
                args={"command": "code ."},
                verification="Verify VS Code opened",
            ))
            steps.append(TaskStep(
                id=2,
                description="Focus VS Code window",
                domain="computer",
                action="focus_window",
                args={"title": "Visual Studio Code"},
                verification="Verify VS Code is foreground window",
            ))

        # 3. Pattern: "Open CodeTantra and write this Java program"
        elif "codetantra" in clean:
            steps.append(TaskStep(
                id=1,
                description="Open browser and navigate to CodeTantra",
                domain="browser",
                action="open_url",
                args={"url": "https://codetantra.com"},
                verification="Verify CodeTantra page loaded",
            ))
            steps.append(TaskStep(
                id=2,
                description="Locate code editor on page",
                domain="browser",
                action="click_element",
                args={"label": "editor"},
                verification="Verify editor focused",
            ))
            steps.append(TaskStep(
                id=3,
                description="Enter Java solution into editor",
                domain="browser",
                action="type_text",
                args={"text": "// Java Solution\npublic class Solution {\n    public static void main(String[] args) {\n        System.out.println(\"Success\");\n    }\n}\n"},
                verification="Verify code entered",
            ))

        # 4. Pattern: "Create a Python file, write this program, run it and tell me the result"
        elif "python" in clean and ("create" in clean or "write" in clean) and "run" in clean:
            steps.append(TaskStep(
                id=1,
                description="Create Python script file 'script.py'",
                domain="file",
                action="create_file",
                args={"path": "script.py", "content": "print('AYRA autonomous test: Execution completed successfully.')\n"},
                verification="Verify file 'script.py' exists",
            ))
            steps.append(TaskStep(
                id=2,
                description="Execute Python script and capture result",
                domain="terminal",
                action="run_command",
                args={"command": "python3 script.py"},
                verification="Verify clean zero exit code",
            ))

        # 5. Pattern: "Find this file and move it to my Documents folder"
        elif "find" in clean and "move" in clean:
            steps.append(TaskStep(
                id=1,
                description="Search filesystem for target file",
                domain="file",
                action="search_files",
                args={"base_dir": ".", "pattern": "*"},
                verification="Verify source file found",
            ))
            steps.append(TaskStep(
                id=2,
                description="Move found file to Documents folder",
                domain="file",
                action="move_file",
                args={"source": "", "destination": "~/Documents"},
                verification="Verify file exists in destination",
            ))

        # 6. Pattern: "Look at this error and fix the project"
        elif "error" in clean and "fix" in clean:
            steps.append(TaskStep(
                id=1,
                description="Inspect screen to observe error message",
                domain="screen",
                action="observe_screen",
                args={},
                verification="Capture visible error text",
            ))
            steps.append(TaskStep(
                id=2,
                description="Run build/test command to isolate failing trace",
                domain="terminal",
                action="run_command",
                args={"command": "pytest -q"},
                verification="Diagnose error traceback",
            ))
            steps.append(TaskStep(
                id=3,
                description="Verify resolution by re-running checks",
                domain="terminal",
                action="run_command",
                args={"command": "pytest -q"},
                verification="Verify tests pass",
            ))

        # 7. Pattern: Generic multi-step web task: "Open this website and complete this task"
        elif any(k in clean for k in ("website", "http", ".com", ".org", "browse")):
            url_match = re.search(r"https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9_\-]+\.(com|org|io|dev)", clean)
            url = url_match.group(0) if url_match else "https://google.com"
            steps.append(TaskStep(
                id=1,
                description=f"Navigate to website '{url}'",
                domain="browser",
                action="open_url",
                args={"url": url},
                verification="Verify web page loaded",
            ))
            steps.append(TaskStep(
                id=2,
                description="Inspect page content and verify elements",
                domain="browser",
                action="inspect_page",
                args={},
                verification="Verify page state",
            ))

        # Fallback general multi-step plan
        else:
            steps.append(TaskStep(
                id=1,
                description=f"Inspect system state for '{goal}'",
                domain="core",
                action="inspect_state",
                args={"goal": goal},
                verification="Verify initial state",
            ))
            steps.append(TaskStep(
                id=2,
                description=f"Execute core action for '{goal}'",
                domain="computer",
                action="execute_action",
                args={"goal": goal},
                verification="Verify goal completion",
            ))

        return TaskPlan(task_id=task_id, goal=goal, steps=steps)

    def replan_step(self, plan: TaskPlan, failed_step: TaskStep, error_reason: str) -> None:
        """Dynamically add or adjust steps when a failure occurs."""
        # e.g., if launching app failed via direct binary, fallback to shell start
        if failed_step.action == "launch_app":
            fallback = TaskStep(
                id=len(plan.steps) + 1,
                description=f"Retry launching '{failed_step.args.get('app_name')}' via system shell fallback",
                domain="terminal",
                action="run_command",
                args={"command": f"start {failed_step.args.get('app_name')}"},
                verification="Verify process started",
            )
            plan.steps.insert(plan.current_step_index + 1, fallback)
        elif failed_step.action == "open_url":
            fallback = TaskStep(
                id=len(plan.steps) + 1,
                description="Focus browser and enter URL via address bar",
                domain="browser",
                action="open_url",
                args=failed_step.args,
                verification="Verify page opened",
            )
            plan.steps.insert(plan.current_step_index + 1, fallback)


# Process-wide planner singleton
_planner_instance: Optional[TaskPlanner] = None


def get_task_planner() -> TaskPlanner:
    global _planner_instance
    if _planner_instance is None:
        _planner_instance = TaskPlanner()
    return _planner_instance
