"""Controlled Terminal & Development Agent for AYRA.

Provides execution of development tools:
- python, python3, pip, pytest
- node, npm
- javac, java
- git
- cargo, go

Features:
- Working directory support
- Command execution with configurable timeouts
- Stdout / stderr / exit code capture
- Output truncation and compression preserving errors, tracebacks, and exit codes
- Process inspection and port queries
- Guardrails against destructive system modification
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool
    summary: str


# Disallowed dangerous binary verbs or flag combinations
_BLOCKED_COMMANDS = [
    r"\b(format\s+[a-z]:|diskpart|mkfs|dd\s+if=)\b",
    r"\b(rm\s+-rf\s+(/|~|\*))\b",
    r"\b(del\s+/[sfq]\s+c:\\windows)\b",
]

# Max characters in output to prevent LLM context explosion (~1.5k tokens)
MAX_OUTPUT_CHARS = 6000


def compress_output(raw_text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Compress long output while keeping first lines, last lines, and errors."""
    if len(raw_text) <= max_chars:
        return raw_text

    lines = raw_text.splitlines()
    if len(lines) <= 60:
        return raw_text[:max_chars] + "\n... [truncated]"

    # Capture first 15 lines and last 35 lines (which usually contain the compiler error / traceback)
    head = lines[:15]
    tail = lines[-35:]
    omitted = len(lines) - 50

    return (
        "\n".join(head)
        + f"\n\n[... {omitted} lines omitted ...]\n\n"
        + "\n".join(tail)
    )


class TerminalAgent:
    """Executes development and system diagnostic commands."""

    def __init__(self, default_cwd: Optional[str] = None) -> None:
        self.default_cwd = default_cwd or str(Path.cwd())

    def _is_command_safe(self, cmd_str: str) -> Tuple[bool, Optional[str]]:
        """Verify command does not contain blocked destructive patterns."""
        clean = cmd_str.lower().strip()
        for pattern in _BLOCKED_COMMANDS:
            if re.search(pattern, clean):
                return False, f"Command contains blocked destructive pattern matching '{pattern}'."
        return True, None

    def execute_command(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout_sec: float = 30.0,
        env: Optional[Dict[str, str]] = None,
    ) -> CommandResult:
        """Execute a shell command with timeout and output capture."""
        safe, reason = self._is_command_safe(command)
        if not safe:
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=reason or "Security violation: blocked command.",
                duration_sec=0.0,
                timed_out=False,
                summary=f"Security violation: {reason}",
            )

        work_dir = cwd or self.default_cwd
        if not Path(work_dir).exists():
            work_dir = str(Path.cwd())

        start_time = time.monotonic()
        timed_out = False
        use_shell = True

        try:
            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)

            proc = subprocess.run(
                command,
                cwd=work_dir,
                shell=use_shell,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=merged_env,
            )
            duration = time.monotonic() - start_time
            stdout_comp = compress_output(proc.stdout)
            stderr_comp = compress_output(proc.stderr)

            status = "succeeded" if proc.returncode == 0 else f"failed (code {proc.returncode})"
            summary = f"Command {status} in {duration:.2f}s."
            if proc.returncode != 0 and stderr_comp:
                summary += f" Error preview: {stderr_comp[:200]}"

            return CommandResult(
                command=command,
                exit_code=proc.returncode,
                stdout=stdout_comp,
                stderr=stderr_comp,
                duration_sec=duration,
                timed_out=False,
                summary=summary,
            )

        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start_time
            out = compress_output(exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or ""))
            err = compress_output(exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr or ""))
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout=out,
                stderr=err + f"\nTimed out after {timeout_sec}s.",
                duration_sec=duration,
                timed_out=True,
                summary=f"Command timed out after {timeout_sec:.1f}s.",
            )

        except Exception as exc:
            duration = time.monotonic() - start_time
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_sec=duration,
                timed_out=False,
                summary=f"Execution error: {exc}",
            )

    def list_processes(self, filter_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List active processes with PID, name, memory, and CPU info."""
        results = []
        try:
            import psutil
            f_clean = filter_name.lower() if filter_name else None
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                try:
                    name = p.info.get("name") or ""
                    if f_clean and f_clean not in name.lower():
                        continue
                    mem = p.info.get("memory_info")
                    mem_mb = round(mem.rss / (1024 * 1024), 1) if mem else 0.0
                    results.append({
                        "pid": p.info["pid"],
                        "name": name,
                        "cpu_percent": p.info.get("cpu_percent", 0.0),
                        "memory_mb": mem_mb,
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return results[:50]

    def list_open_ports(self) -> List[Dict[str, Any]]:
        """List listening network ports and binding PIDs."""
        ports = []
        try:
            import psutil
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == "LISTEN":
                    ports.append({
                        "ip": conn.laddr.ip,
                        "port": conn.laddr.port,
                        "pid": conn.pid,
                    })
        except Exception:
            pass
        return ports[:30]


# Process-wide terminal agent
_terminal_agent_instance: Optional[TerminalAgent] = None


def get_terminal_agent() -> TerminalAgent:
    global _terminal_agent_instance
    if _terminal_agent_instance is None:
        _terminal_agent_instance = TerminalAgent()
    return _terminal_agent_instance
