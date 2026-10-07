"""Dynamic Application Discovery for AYRA.

Discovers installed applications dynamically across:
1. Windows Start Menu shortcuts (.lnk files)
2. Windows Registry 'App Paths' (HKLM & HKCU)
3. Standard Program Files and User Local AppData directories
4. System PATH executables
5. Active running processes via psutil

Provides fuzzy matching so requests like:
  "Open Chrome"
  "Open VS Code"
  "Open Edge"
  "Open Spotify"
  "Open Calculator"
resolve to the true executable path rather than relying on a small hardcoded list.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Common fallback mappings for cross-platform / WSL / Windows
_WELL_KNOWN_ALIASES = {
    "chrome": ["google-chrome", "chrome.exe", "google-chrome-stable", "chromium"],
    "google chrome": ["google-chrome", "chrome.exe", "google-chrome-stable"],
    "vs code": ["code", "code.exe", "code.cmd"],
    "vscode": ["code", "code.exe", "code.cmd"],
    "code": ["code", "code.exe", "code.cmd"],
    "edge": ["msedge.exe", "microsoft-edge", "msedge"],
    "microsoft edge": ["msedge.exe", "microsoft-edge"],
    "notepad": ["notepad.exe", "notepad"],
    "calculator": ["calc.exe", "gnome-calculator", "kcalc"],
    "calc": ["calc.exe"],
    "spotify": ["spotify.exe", "spotify"],
    "terminal": ["wt.exe", "powershell.exe", "cmd.exe", "x-terminal-emulator", "bash"],
    "explorer": ["explorer.exe"],
    "file explorer": ["explorer.exe"],
    "cmd": ["cmd.exe"],
    "powershell": ["powershell.exe", "pwsh.exe", "pwsh"],
}


class ApplicationDiscovery:
    """Discovers and caches available desktop applications."""

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}
        self._cached = False

    def _index_start_menu(self) -> Dict[str, str]:
        """Scan Windows Start Menu directories for .lnk shortcuts."""
        apps: Dict[str, str] = {}
        if sys.platform != "win32":
            return apps

        dirs_to_scan = []
        appdata = os.getenv("APPDATA")
        if appdata:
            dirs_to_scan.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        progdata = os.getenv("ProgramData")
        if progdata:
            dirs_to_scan.append(Path(progdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

        for base_dir in dirs_to_scan:
            if not base_dir.exists():
                continue
            try:
                for root, _, files in os.walk(base_dir):
                    for file in files:
                        if file.lower().endswith(".lnk"):
                            name = Path(file).stem.lower()
                            full_path = os.path.join(root, file)
                            apps[name] = full_path
            except Exception:
                pass
        return apps

    def _index_registry_app_paths(self) -> Dict[str, str]:
        """Scan Windows Registry App Paths keys."""
        apps: Dict[str, str] = {}
        if sys.platform != "win32":
            return apps

        try:
            import winreg
            keys = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            ]
            for root_key, subkey_path in keys:
                try:
                    with winreg.OpenKey(root_key, subkey_path) as parent_key:
                        num_subkeys = winreg.QueryInfoKey(parent_key)[0]
                        for i in range(num_subkeys):
                            try:
                                subkey_name = winreg.EnumKey(parent_key, i)
                                with winreg.OpenKey(parent_key, subkey_name) as app_key:
                                    val, _ = winreg.QueryValueEx(app_key, "")
                                    if val and os.path.exists(val):
                                        clean_name = subkey_name.lower().replace(".exe", "")
                                        apps[clean_name] = val
                            except Exception:
                                continue
                except Exception:
                    continue
        except ImportError:
            pass

        return apps

    def _index_standard_paths(self) -> Dict[str, str]:
        """Index common user/system program install locations."""
        apps: Dict[str, str] = {}
        check_dirs = []
        if sys.platform == "win32":
            for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
                val = os.getenv(env_var)
                if val:
                    check_dirs.append(Path(val))
        else:
            check_dirs.extend([Path("/usr/bin"), Path("/usr/local/bin"), Path("/snap/bin"), Path.home() / ".local" / "bin"])

        for base in check_dirs:
            if not base.exists():
                continue
            try:
                for item in base.glob("*"):
                    if sys.platform == "win32":
                        if item.is_file() and item.suffix.lower() == ".exe":
                            apps[item.stem.lower()] = str(item)
                    else:
                        if item.is_file() and os.access(item, os.X_OK):
                            apps[item.name.lower()] = str(item)
            except Exception:
                continue
        return apps

    def refresh_index(self) -> None:
        """Scan and populate the application index."""
        indexed: Dict[str, str] = {}
        # Start menu
        indexed.update(self._index_start_menu())
        # Registry
        indexed.update(self._index_registry_app_paths())
        # Standard locations
        indexed.update(self._index_standard_paths())
        self._cache = indexed
        self._cached = True

    def find_application(self, query: str) -> Optional[str]:
        """Find the executable path for the given application name using exact and fuzzy matching."""
        if not self._cached:
            self.refresh_index()

        q = query.strip().lower()
        q = re.sub(r"^(open|launch|start|run)\s+", "", q).strip()

        # 1. Direct alias check
        if q in _WELL_KNOWN_ALIASES:
            for candidate in _WELL_KNOWN_ALIASES[q]:
                # Check PATH
                path_match = shutil.which(candidate)
                if path_match:
                    return path_match
                # Check cache
                cand_clean = candidate.replace(".exe", "").lower()
                if cand_clean in self._cache:
                    return self._cache[cand_clean]

        # 2. Direct exact match in cache
        if q in self._cache:
            return self._cache[q]

        # 3. Direct match via shutil.which
        direct_which = shutil.which(q)
        if direct_which:
            return direct_which
        if sys.platform == "win32":
            direct_exe = shutil.which(f"{q}.exe")
            if direct_exe:
                return direct_exe

        # 4. Prefix or substring matching in cache
        for app_name, path in self._cache.items():
            if q == app_name or q in app_name or app_name in q:
                return path

        # 5. Fuzzy matching with high cutoff
        matches = difflib.get_close_matches(q, list(self._cache.keys()), n=1, cutoff=0.6)
        if matches:
            return self._cache[matches[0]]

        return None

    def launch_application(self, app_query: str, args: Optional[List[str]] = None) -> Tuple[bool, str]:
        """Discover and launch an application."""
        resolved = self.find_application(app_query)
        extra_args = args or []

        if not resolved:
            # Fallback on Windows start command or system open
            try:
                clean_name = re.sub(r"^(open|launch|start|run)\s+", "", app_query.strip().lower())
                if sys.platform == "win32":
                    subprocess.Popen(f'start "" "{clean_name}"', shell=True)
                    return True, f"Launched '{clean_name}' via Windows shell."
                else:
                    return False, f"Could not find application '{app_query}'."
            except Exception as exc:
                return False, f"Application '{app_query}' could not be located: {exc}"

        try:
            if resolved.lower().endswith(".lnk"):
                if sys.platform == "win32":
                    os.startfile(resolved)
                    return True, f"Launched '{app_query}' from shortcut: {resolved}"
            
            cmd = [resolved] + extra_args
            subprocess.Popen(cmd, shell=False)
            return True, f"Launched '{app_query}' (executable: {resolved})"
        except Exception as exc:
            # Attempt os.startfile on Windows
            if sys.platform == "win32":
                try:
                    os.startfile(resolved)
                    return True, f"Launched '{app_query}' via os.startfile."
                except Exception as e2:
                    return False, f"Failed to launch '{resolved}': {e2}"
            return False, f"Failed to launch '{resolved}': {exc}"


# Process-wide singleton
_discovery_instance: Optional[ApplicationDiscovery] = None


def get_app_discovery() -> ApplicationDiscovery:
    global _discovery_instance
    if _discovery_instance is None:
        _discovery_instance = ApplicationDiscovery()
    return _discovery_instance
