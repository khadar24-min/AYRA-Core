"""File Agent for AYRA.

Provides full, safe filesystem capabilities:
- Search files (recursive glob, name patterns)
- Read files (with credential protection and size bounds)
- Create directories & files
- Write files
- Surgical line-level editing (replacing specific target snippets)
- Copy, move, and rename files
- Safe deletion (Tier 3 confirmation required)
- Directory inspection and content comparison
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Credential protection patterns (inherited and reinforced from file_reader)
_DENIED_EXACT_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ed25519_sk", "id_ecdsa_sk",
    "sam", "security", "system", "ntuser.dat", ".htpasswd",
}
_DENIED_SUFFIXES = {
    ".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".kdbx", ".ppk", ".asc", ".gpg",
}


def _is_path_safe(path: Path) -> Tuple[bool, Optional[str]]:
    """Verify that path does not point to credential stores or critical OS directories."""
    resolved = path.resolve()
    name = resolved.name.lower()

    if name in _DENIED_EXACT_NAMES or any(name.endswith(sfx) for sfx in _DENIED_SUFFIXES):
        return False, f"Access to sensitive credential file '{name}' is denied."

    resolved_str = str(resolved).lower()
    if sys.platform == "win32":
        if re.search(r"^[a-z]:\\(windows|system32|recovery|boot)", resolved_str):
            return False, f"Path '{resolved}' is in a protected system directory."
    else:
        if resolved_str.startswith(("/etc", "/boot", "/sys", "/proc")):
            return False, f"Path '{resolved}' is in a protected system directory."

    return True, None


class FileAgent:
    """Handles controlled filesystem operations."""

    def resolve_path(self, path_str: str) -> Path:
        """Expand user ~ and resolve absolute path."""
        p = Path(path_str).expanduser()
        return p.resolve()

    def search_files(
        self,
        base_dir: str,
        pattern: str = "*",
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """Recursively search directory for files matching pattern."""
        results: List[Dict[str, Any]] = []
        root = self.resolve_path(base_dir)
        if not root.exists() or not root.is_dir():
            return results

        try:
            for item in root.rglob(pattern):
                if len(results) >= max_results:
                    break
                try:
                    is_safe, _ = _is_path_safe(item)
                    if not is_safe:
                        continue
                    stat = item.stat()
                    results.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": item.is_dir(),
                        "size_bytes": stat.st_size if item.is_file() else 0,
                    })
                except Exception:
                    continue
        except Exception as exc:
            print(f"[file_agent] search error: {exc}", file=sys.stderr)
        return results

    def read_file(self, path_str: str, max_bytes: int = 64 * 1024) -> Tuple[bool, str]:
        """Read text contents of a file."""
        p = self.resolve_path(path_str)
        if not p.exists():
            return False, f"File '{p}' does not exist."
        if not p.is_file():
            return False, f"Path '{p}' is a directory, not a file."

        is_safe, reason = _is_path_safe(p)
        if not is_safe:
            return False, reason or "Access denied."

        try:
            with open(p, "rb") as f:
                header = f.read(min(max_bytes, 8192))
                if b"\x00" in header:
                    return False, f"File '{p.name}' is binary and cannot be read as text."
                f.seek(0)
                raw = f.read(max_bytes)
                text = raw.decode("utf-8", errors="replace")
                return True, text
        except Exception as exc:
            return False, f"Failed to read '{p}': {exc}"

    def create_file(self, path_str: str, content: str = "") -> Tuple[bool, str]:
        """Create a file, creating parent directories if needed."""
        p = self.resolve_path(path_str)
        is_safe, reason = _is_path_safe(p)
        if not is_safe:
            return False, reason or "Access denied."

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            return True, f"Created file '{p}' ({len(content)} chars written)."
        except Exception as exc:
            return False, f"Failed to create file '{p}': {exc}"

    def write_file(self, path_str: str, content: str) -> Tuple[bool, str]:
        """Write or overwrite content of an existing or new file."""
        return self.create_file(path_str, content)

    def edit_file(self, path_str: str, target_snippet: str, replacement_snippet: str) -> Tuple[bool, str]:
        """Surgically replace target_snippet with replacement_snippet in a file."""
        ok, current_text = self.read_file(path_str)
        if not ok:
            return False, current_text

        if target_snippet not in current_text:
            return False, f"Target snippet not found in '{path_str}'. No changes made."

        count = current_text.count(target_snippet)
        if count > 1:
            return False, f"Target snippet appears {count} times in '{path_str}'. Target must be unique."

        new_text = current_text.replace(target_snippet, replacement_snippet, 1)
        return self.write_file(path_str, new_text)

    def copy_file(self, source_str: str, destination_str: str) -> Tuple[bool, str]:
        """Copy a file or directory."""
        src = self.resolve_path(source_str)
        dst = self.resolve_path(destination_str)

        is_safe_src, r_src = _is_path_safe(src)
        if not is_safe_src:
            return False, r_src or "Source access denied."

        is_safe_dst, r_dst = _is_path_safe(dst)
        if not is_safe_dst:
            return False, r_dst or "Destination access denied."

        if not src.exists():
            return False, f"Source '{src}' does not exist."

        try:
            if dst.is_dir():
                dst = dst / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            return True, f"Copied '{src}' to '{dst}'."
        except Exception as exc:
            return False, f"Copy failed: {exc}"

    def move_file(self, source_str: str, destination_str: str) -> Tuple[bool, str]:
        """Move or rename a file or directory."""
        src = self.resolve_path(source_str)
        dst = self.resolve_path(destination_str)

        is_safe_src, r_src = _is_path_safe(src)
        if not is_safe_src:
            return False, r_src or "Source access denied."

        is_safe_dst, r_dst = _is_path_safe(dst)
        if not is_safe_dst:
            return False, r_dst or "Destination access denied."

        if not src.exists():
            return False, f"Source '{src}' does not exist."

        try:
            if dst.is_dir():
                dst = dst / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return True, f"Moved '{src}' to '{dst}'."
        except Exception as exc:
            return False, f"Move failed: {exc}"

    def rename_file(self, source_str: str, new_name: str) -> Tuple[bool, str]:
        """Rename a file in its current directory."""
        src = self.resolve_path(source_str)
        dst = src.parent / new_name
        return self.move_file(str(src), str(dst))

    def delete_file(self, path_str: str, confirmed: bool = False) -> Tuple[bool, str]:
        """Permanently delete a file or directory (Tier 3 confirmation gated)."""
        p = self.resolve_path(path_str)
        if not confirmed:
            return False, f"Deletion of '{p}' requires explicit confirmation (confirmed=True)."

        is_safe, reason = _is_path_safe(p)
        if not is_safe:
            return False, reason or "Access denied."

        if not p.exists():
            return False, f"Path '{p}' does not exist."

        try:
            if p.is_dir():
                shutil.rmtree(p)
                return True, f"Deleted directory '{p}'."
            else:
                p.unlink()
                return True, f"Deleted file '{p}'."
        except Exception as exc:
            return False, f"Deletion failed: {exc}"

    def inspect_directory(self, dir_str: str) -> Tuple[bool, List[Dict[str, Any]]]:
        """List files and subdirectories in a directory."""
        p = self.resolve_path(dir_str)
        if not p.exists() or not p.is_dir():
            return False, []

        entries = []
        try:
            for item in p.iterdir():
                entries.append({
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size_bytes": item.stat().st_size if item.is_file() else 0,
                })
            return True, entries
        except Exception:
            return False, []


# Process-wide file agent
_file_agent_instance: Optional[FileAgent] = None


def get_file_agent() -> FileAgent:
    global _file_agent_instance
    if _file_agent_instance is None:
        _file_agent_instance = FileAgent()
    return _file_agent_instance
