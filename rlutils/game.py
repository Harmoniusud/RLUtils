"""Processes and launching: whether Rocket League runs, where it is installed, and starting it on an Epic
account (with the Epic account switcher).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path


CREATE_NO_WINDOW = 0x08000000


class ReadError(Exception):
    """Launching failed; the message is shown to the user."""


# ====================================================================================================
# Processes: what is running.
# ====================================================================================================


def _process_name(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.wintypes.DWORD), ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.wintypes.DWORD), ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_names() -> set[str]:
    """Lower-case exe names of all running processes, from a Toolhelp snapshot (a few ms; tasklist took about
    200 ms, which froze the window when it ran on the UI thread)."""
    if sys.platform != "win32":
        return set()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return set()
    names = set()
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = kernel32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = kernel32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))
    return names


def process_running(exe_name: str) -> bool:
    """Read-only check of the process list for an exe name, e.g. "RocketLeague.exe". Windows only."""
    return exe_name.lower() in process_names()


# ====================================================================================================
# Launch: Epic accounts, with the Epic account switcher (EpicSwitcher.ps1: built into the app, or the user's
# own) swapping the launcher's saved login. `store` = the folder the built-in one keeps accounts in (passed as
# -StoreDir); None for the user's own script, which keeps them next to itself.
# ====================================================================================================


def _powershell_file(script: Path, *args: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=CREATE_NO_WINDOW)


def _store_args(store: Path | None) -> tuple[str, ...]:
    return ("-StoreDir", str(store)) if store else ()


def switcher_accounts(script: Path, store: Path | None = None) -> list[dict]:
    """[{id, name, active}] saved in the account switcher."""
    if not script.exists():
        raise ReadError(f"Account switcher not found: {script}")
    try:
        res = _powershell_file(script, "-ListJson", *_store_args(store), timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise ReadError("the account switcher didn't answer") from exc
    text = res.stdout.strip()
    start = text.find("[")
    if res.returncode != 0 or start < 0:
        raise ReadError((res.stderr or res.stdout or "the account switcher failed").strip()[:300])
    try:
        rows = json.loads(text[start:])
    except ValueError as exc:
        raise ReadError("unexpected answer from the account switcher") from exc
    if isinstance(rows, dict):
        rows = [rows]
    return [{"id": str(r.get("Id", "")), "name": str(r.get("Name", "")), "active": bool(r.get("Active"))}
            for r in rows if r.get("Id")]


def switcher_command(script: Path, *args: str, store: Path | None = None, timeout: float = 120) -> str:
    """Run the account switcher with command-line options. Returns its last line of output (what it did);
    raises ReadError with that line when it fails."""
    if not script.exists():
        raise ReadError(f"Account switcher not found: {script}")
    try:
        res = _powershell_file(script, *args, *_store_args(store), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ReadError("the account switcher took too long") from exc
    lines = [l.strip() for l in (res.stdout or "").splitlines() if l.strip()]
    msg = lines[-1] if lines else ""
    if res.returncode != 0:
        raise ReadError(msg or (res.stderr or "the account switcher failed").strip()[:300])
    return msg


def switch_account(script: Path, account_id: str, timeout_s: int, store: Path | None = None) -> str:
    """Switch the Epic launcher to `account_id` and wait until it has signed in."""
    return switcher_command(script, "-SwitchTo", account_id, "-WaitSignIn", str(int(timeout_s)),
                            store=store, timeout=timeout_s + 120)


def add_new_account(script: Path, store: Path | None = None) -> str:
    """Restart the Epic launcher on its sign-in screen, to sign into another account."""
    return switcher_command(script, "-AddNew", store=store)


def save_current_account(script: Path, store: Path | None = None) -> str:
    """Save the account the Epic launcher is signed into now in the account switcher."""
    return switcher_command(script, "-SaveCurrent", store=store, timeout=60)


def start_program(path: Path) -> None:
    """Start an exe the way a double-click would, with its own folder as the working directory."""
    if not path.exists():
        raise ReadError(f"Not found: {path}")
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True, creationflags=flags)


def wait_for(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ====================================================================================================
# Game: where Rocket League is installed, and starting it.
#
# Epic: the launcher's install manifests (ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item, JSON, with
# AppName "Sugar" for Rocket League). It is started through the launcher's link, which signs it in; the exe
# alone would start signed out. Steam: Steam's library folders (steamapps\libraryfolders.vdf) and the app's
# manifest (appmanifest_252950.acf), started through steam://. Or a file the user picks: an exe, a Windows
# shortcut (.lnk) or an internet shortcut (.url), started as a double-click would.
# ====================================================================================================

EPIC_APP = "Sugar"
EPIC_LAUNCH = "com.epicgames.launcher://apps/Sugar?action=launch&silent=true"
STEAM_APP = "252950"
STEAM_LAUNCH = f"steam://rungameid/{STEAM_APP}"
GAME_FILES = (".exe", ".lnk", ".url")


def _programdata() -> Path:
    return Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")


def find_epic(manifests: Path | None = None) -> dict | None:
    """{"method": "epic", "dir", "exe"} from the Epic launcher's install manifests, or None."""
    folder = manifests or _programdata() / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    try:
        items = sorted(folder.glob("*.item"))
    except OSError:
        return None
    for item in items:
        try:
            info = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or info.get("AppName") != EPIC_APP:
            continue
        where = Path(str(info.get("InstallLocation") or ""))
        exe = where / "Binaries" / "Win64" / "RocketLeague.exe"
        if where.is_dir():
            return {"method": "epic", "dir": str(where), "exe": str(exe) if exe.exists() else ""}
    return None


def _steam_root() -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg
    for hive, key, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(hive, key) as k:
                value = winreg.QueryValueEx(k, name)[0]
        except OSError:
            continue
        if value and Path(value).is_dir():
            return Path(value)
    return None


def _vdf_values(text: str, key: str) -> list[str]:
    """Every "key"  "value" pair's value in a Valve KeyValues (.vdf/.acf) text."""
    import re
    return [v.replace("\\\\", "\\") for v in re.findall(rf'"{re.escape(key)}"\s+"((?:[^"\\]|\\.)*)"', text)]


def find_steam(root: Path | None = None) -> dict | None:
    """{"method": "steam", "dir", "exe"} from Steam's library folders, or None."""
    root = root or _steam_root()
    if not root:
        return None
    libraries = [root]
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libraries += [Path(p) for p in _vdf_values(text, "path")]
    except OSError:
        pass
    for lib in dict.fromkeys(libraries):
        try:
            acf = (lib / "steamapps" / f"appmanifest_{STEAM_APP}.acf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = _vdf_values(acf, "installdir")
        where = lib / "steamapps" / "common" / (names[0] if names else "rocketleague")
        if where.is_dir():
            exe = where / "Binaries" / "Win64" / "RocketLeague.exe"
            return {"method": "steam", "dir": str(where), "exe": str(exe) if exe.exists() else ""}
    return None


def find_installs() -> list[dict]:
    """Rocket League installs found on this PC: Epic first, then Steam."""
    return [found for found in (find_epic(), find_steam()) if found]


def check_game_file(path: Path) -> str:
    """'' if `path` can start the game (an exe or shortcut that exists), else what's wrong."""
    if path.suffix.lower() not in GAME_FILES:
        return f"Pick a program (.exe) or a shortcut (.lnk, .url), not a {path.suffix or 'file without a type'}."
    if not path.exists():
        return f"Not found: {path}"
    return ""


def resolve_launch(method: str, shortcut: Path | None, installs: list[dict] | None = None) -> tuple[str, str]:
    """(how, target): ("uri", link) or ("file", path) to start the game with, for a launch.method. "auto"
    takes the Epic install, else the Steam one. Raises ReadError when there's nothing to start."""
    if method == "path":
        if not shortcut:
            raise ReadError("No game or shortcut chosen. Pick one in Settings > Rocket League.")
        problem = check_game_file(shortcut)
        if problem:
            raise ReadError(problem)
        return "file", str(shortcut)
    installs = find_installs() if installs is None else installs
    found = {i["method"]: i for i in installs}
    if method == "epic" or (method == "auto" and "epic" in found):
        if method == "epic" and "epic" not in found:
            raise ReadError("Rocket League isn't installed through the Epic Games launcher here.")
        return "uri", EPIC_LAUNCH
    if method == "steam" or (method == "auto" and "steam" in found):
        if method == "steam" and "steam" not in found:
            raise ReadError("Rocket League isn't installed through Steam here.")
        return "uri", STEAM_LAUNCH
    raise ReadError("Rocket League wasn't found. Run setup (Settings) to find it, or pick the game or a shortcut.")


def start_game(how: str, target: str) -> None:
    """Start the game: a launcher link, or a file the way a double-click would (shortcuts too)."""
    if how == "file" and Path(target).suffix.lower() == ".exe":
        start_program(Path(target))
    elif sys.platform == "win32":
        os.startfile(target)  # opens .lnk/.url files and com.epicgames.launcher:// / steam:// links
    else:
        raise ReadError("Starting the game only works on Windows.")
