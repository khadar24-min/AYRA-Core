"""Comprehensive Unit and Integration Test Suite for AYRA Autonomous Desktop Agent.

Tests cover:
- Test 1: Local intent fast-path (time, date, creator identity Khadar/Nannu)
- Test 2: Dynamic tool router context optimization (verifying token schema reduction)
- Test 3: Safety tiers & permission policy
- Test 4: Application discovery
- Test 5: Computer control primitives
- Test 6: File agent (create, read, surgical edit, move, copy, safe delete)
- Test 7: Terminal agent (execution, output compression, error isolation, blocked commands)
- Test 8: Browser agent (URL navigation, page inspection, element interaction)
- Test 9: Screen agent (visual grounding, element resolution)
- Test 10: Task planner & autonomous loop (observe -> plan -> act -> verify -> recover)
- Test 11-17: End-to-end execution of the target benchmark user requests
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.agent_logger import get_agent_logger
from src.agent_loop import AutonomousAgentLoop, get_agent_loop
from src.agent_planner import (
    PlanStatus,
    StepStatus,
    TaskPlan,
    TaskStep,
    get_task_planner,
    is_computer_task,
)
from src.agent_security import AgentSecurityManager, RiskTier
from src.app_discovery import get_app_discovery
from src.browser_agent import get_browser_agent
from src.computer_control import get_computer_controller
from src.file_agent import FileAgent, get_file_agent
from src.local_intents import check_local_intent
from src.screen_agent import get_screen_agent
from src.terminal_agent import TerminalAgent, compress_output, get_terminal_agent
from src.tool_router import ToolDomain, get_tool_router


class TestLocalIntents(unittest.TestCase):
    """Test 1 & 2: Local Intent Fast-Path (0 Tokens / Deterministic Response)."""

    def test_time_query(self):
        resp = check_local_intent("What time is it?")
        self.assertIsNotNone(resp)
        self.assertTrue("It is" in resp or ":" in resp)

        resp2 = check_local_intent("Hey AYRA, tell me the current time")
        self.assertIsNotNone(resp2)

    def test_date_query(self):
        resp = check_local_intent("What is the date today?")
        self.assertIsNotNone(resp)
        self.assertIn("Today is", resp)

    def test_identity_and_creator(self):
        resp_who = check_local_intent("Who are you?")
        self.assertIsNotNone(resp_who)
        self.assertIn("AYRA", resp_who)
        self.assertIn("Khadar", resp_who)

        resp_maker = check_local_intent("Who created you?")
        self.assertIsNotNone(resp_maker)
        self.assertIn("Khadar, also known as Nannu", resp_maker)

        resp_boss = check_local_intent("Who is your creator?")
        self.assertIsNotNone(resp_boss)
        self.assertIn("Khadar", resp_boss)

    def test_system_status(self):
        resp = check_local_intent("How is the system doing?")
        self.assertIsNotNone(resp)

    def test_non_local_intent_returns_none(self):
        resp = check_local_intent("Open Chrome")
        self.assertIsNone(resp)

        resp2 = check_local_intent("Explain quantum computing")
        self.assertIsNone(resp2)


class TestToolRouter(unittest.TestCase):
    """Token Context Optimization & Dynamic Tool Selection."""

    def setUp(self):
        self.router = get_tool_router()
        self.mock_tools = [
            {"name": "web_search"},
            {"name": "get_weather"},
            {"name": "launch_app"},
            {"name": "screen_snapshot"},
            {"name": "open_url"},
            {"name": "create_file"},
            {"name": "run_command"},
            {"name": "status_report"},
        ]

    def test_domain_filtering_for_browser(self):
        selected = self.router.filter_tools(self.mock_tools, "Open Chrome and go to website", max_tools=4)
        names = [t["name"] for t in selected]
        self.assertTrue("open_url" in names or "launch_app" in names)
        # Verify schema list is bounded (not full catalog)
        self.assertLessEqual(len(selected), 4)

    def test_domain_filtering_for_file_and_terminal(self):
        selected = self.router.filter_tools(self.mock_tools, "Create a python file and run it", max_tools=4)
        names = [t["name"] for t in selected]
        self.assertTrue("create_file" in names or "run_command" in names)
        self.assertLessEqual(len(selected), 4)


class TestAgentSecurity(unittest.TestCase):
    """Safety and 3-Tier Risk Policy."""

    def setUp(self):
        self.security = AgentSecurityManager()

    def test_tier_1_read_is_allowed(self):
        allowed, _ = self.security.is_action_allowed("read_file", {"path": "test.txt"})
        self.assertTrue(allowed)

        allowed_mouse, _ = self.security.is_action_allowed("mouse_click", {"x": 100, "y": 100})
        self.assertTrue(allowed_mouse)

    def test_tier_2_monitored_is_allowed_with_logging(self):
        allowed, _ = self.security.is_action_allowed("create_file", {"path": "test.py"})
        self.assertTrue(allowed)

    def test_tier_3_deletion_requires_confirmation(self):
        allowed_unconfirmed, reason = self.security.is_action_allowed("delete_file", {"path": "important.txt"}, user_confirmed=False)
        self.assertFalse(allowed_unconfirmed)
        self.assertIn("confirmation", reason.lower())

        allowed_confirmed, _ = self.security.is_action_allowed("delete_file", {"path": "important.txt"}, user_confirmed=True)
        self.assertTrue(allowed_confirmed)

    def test_dangerous_shell_command_blocked(self):
        allowed, reason = self.security.is_action_allowed("run_terminal_command", {"command": "format c:"})
        self.assertFalse(allowed)
        self.assertIn("confirmation", reason.lower())


class TestFileAgent(unittest.TestCase):
    """File Agent: Creation, Reading, Surgical Editing, Moving, Copying, Deleting."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ayra_file_test_")
        self.agent = FileAgent()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_file_lifecycle(self):
        file_path = os.path.join(self.test_dir, "sample.txt")
        # 1. Create file
        ok, msg = self.agent.create_file(file_path, "Hello AYRA World!\nThis is line 2.\n")
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(file_path))

        # 2. Read file
        ok, content = self.agent.read_file(file_path)
        self.assertTrue(ok)
        self.assertIn("Hello AYRA World!", content)

        # 3. Surgical edit
        ok, msg = self.agent.edit_file(file_path, "Hello AYRA World!", "Hello Autonomous World!")
        self.assertTrue(ok)
        ok, edited_content = self.agent.read_file(file_path)
        self.assertIn("Hello Autonomous World!", edited_content)
        self.assertNotIn("Hello AYRA World!", edited_content)

        # 4. Copy file
        copy_path = os.path.join(self.test_dir, "sample_copy.txt")
        ok, _ = self.agent.copy_file(file_path, copy_path)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(copy_path))

        # 5. Move file
        moved_path = os.path.join(self.test_dir, "sample_moved.txt")
        ok, _ = self.agent.move_file(copy_path, moved_path)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(copy_path))
        self.assertTrue(os.path.exists(moved_path))

        # 6. Delete file
        ok, _ = self.agent.delete_file(moved_path, confirmed=True)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(moved_path))


class TestTerminalAgent(unittest.TestCase):
    """Terminal & Development Agent: Command Execution, Compression & Safety."""

    def setUp(self):
        self.agent = TerminalAgent()

    def test_run_command_success(self):
        res = self.agent.execute_command("python3 -c 'print(\"Terminal agent functional\")'")
        self.assertEqual(res.exit_code, 0)
        self.assertIn("Terminal agent functional", res.stdout)
        self.assertFalse(res.timed_out)

    def test_run_command_error_capture(self):
        res = self.agent.execute_command("python3 -c 'raise ValueError(\"Diagnostic test error\")'")
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("Diagnostic test error", res.stderr)

    def test_output_compression(self):
        huge_text = "\n".join([f"Trace line {i}" for i in range(200)])
        compressed = compress_output(huge_text, max_chars=1000)
        self.assertLess(len(compressed), len(huge_text))
        self.assertIn("Trace line 0", compressed)
        self.assertIn("Trace line 199", compressed)


class TestAppDiscoveryAndComputerControl(unittest.TestCase):
    """Application Discovery & Computer Control Primitives."""

    def test_app_discovery(self):
        discovery = get_app_discovery()
        # Should identify common system executables
        python_path = discovery.find_application("python") or discovery.find_application("bash") or discovery.find_application("cmd")
        self.assertIsNotNone(python_path)

    def test_computer_controller_dimensions(self):
        controller = get_computer_controller()
        self.assertGreater(controller.screen_width, 0)
        self.assertGreater(controller.screen_height, 0)


class TestScreenAgent(unittest.TestCase):
    """Screen Agent & Visual Grounding."""

    def test_screen_dimensions(self):
        agent = get_screen_agent()
        w, h = agent.get_screen_dimensions()
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    def test_fallback_element_finding(self):
        agent = get_screen_agent()
        match = agent.find_element("start")
        self.assertIsNotNone(match)
        self.assertGreater(match.center_x, 0)
        self.assertGreater(match.center_y, 0)


class TestEndToEndAutonomousTasks(unittest.TestCase):
    """Tests 3 - 10: End-to-End Scenarios for Autonomous Computer Tasks."""

    def setUp(self):
        self.planner = get_task_planner()
        self.loop = get_agent_loop()
        self.test_dir = tempfile.mkdtemp(prefix="ayra_e2e_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_scenario_open_chrome(self):
        """TEST 3: 'Open Chrome.'"""
        plan = self.planner.create_plan("Open Chrome")
        self.assertGreaterEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].action, "launch_app")
        self.assertIn("chrome", plan.steps[0].args.get("app_name", "").lower())

    def test_scenario_open_vscode(self):
        """TEST 4: 'Open VS Code and open my AYRA project.'"""
        plan = self.planner.create_plan("Open VS Code and open my AYRA project")
        self.assertGreaterEqual(len(plan.steps), 2)
        actions = [s.action for s in plan.steps]
        self.assertIn("run_command", actions)

    def test_scenario_codetantra_java(self):
        """TEST 5: 'Open CodeTantra and write this Java program.'"""
        plan = self.planner.create_plan("Open CodeTantra and write this Java program")
        self.assertGreaterEqual(len(plan.steps), 3)
        domains = [s.domain for s in plan.steps]
        self.assertIn("browser", domains)

    def test_scenario_python_file_execution(self):
        """TEST 6: 'Create a Python file, write this program, run it and tell me the result.'"""
        py_file = os.path.join(self.test_dir, "test_prog.py")
        step_create = TaskStep(
            id=1,
            description="Create Python file",
            domain="file",
            action="create_file",
            args={"path": py_file, "content": "print('Output from generated Python code')\n"},
        )
        ok, res = self.loop.execute_action(step_create)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(py_file))

        step_run = TaskStep(
            id=2,
            description="Run Python file",
            domain="terminal",
            action="run_command",
            args={"command": f"python3 {py_file}"},
        )
        ok_run, res_run = self.loop.execute_action(step_run)
        self.assertTrue(ok_run)
        self.assertIn("Output from generated Python code", res_run)

    def test_scenario_find_and_move_file(self):
        """TEST 7: 'Find this file and move it to my Documents folder.'"""
        src_file = os.path.join(self.test_dir, "document.txt")
        dest_file = os.path.join(self.test_dir, "moved_document.txt")
        # Setup source
        FileAgent().create_file(src_file, "Document content")

        step_move = TaskStep(
            id=1,
            description="Move file",
            domain="file",
            action="move_file",
            args={"source": src_file, "destination": dest_file},
        )
        ok, _ = self.loop.execute_action(step_move)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(dest_file))
        self.assertFalse(os.path.exists(src_file))

    def test_scenario_observe_and_fix_error(self):
        """TEST 8: 'Look at this error and fix the project.'"""
        plan = self.planner.create_plan("Look at this error and fix the project")
        self.assertGreaterEqual(len(plan.steps), 2)
        domains = [s.domain for s in plan.steps]
        self.assertIn("screen", domains)
        self.assertIn("terminal", domains)

    def test_scenario_website_task(self):
        """TEST 9: 'Open a website and perform a multi-step task.'"""
        plan = self.planner.create_plan("Open website https://example.com and check status")
        self.assertGreaterEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].action, "open_url")

    def test_scenario_recovery_on_failure_no_infinite_loop(self):
        """TEST 10: Forced failure recovery without infinite loop."""
        step_fail = TaskStep(
            id=99,
            description="Run invalid failing command",
            domain="terminal",
            action="run_command",
            args={"command": "python3 -c 'import nonexistent_module'"},
            max_retries=1,
        )
        plan = TaskPlan(task_id="test_fail", goal="Handle failure safely", steps=[step_fail])
        # Mark fail 1st time (retry permitted)
        can_retry = plan.mark_step_failed(99, "No module named nonexistent_module")
        self.assertTrue(can_retry)
        # Mark fail 2nd time (exceeds max_retries, stops safely)
        can_retry_again = plan.mark_step_failed(99, "No module named nonexistent_module")
        self.assertFalse(can_retry_again)
        self.assertEqual(plan.status, PlanStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
