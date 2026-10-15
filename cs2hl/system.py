"""Operating-system differences: where Steam, CS2 and the workspace live, and how to start, stop and open things.

CS2 runs natively on Windows and Linux. Valve does not ship CS2 for macOS, so on a Mac the app can only analyse
demos; recording is refused with an explanation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
# Running from the packaged Windows/Linux executable (PyInstaller) rather than from source.
FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

# Keep console windows from flashing up for helper programs (tasklist, ffmpeg...) on Windows.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
# Same, at lower CPU priority, for work that runs alongside CS2 while it records (see lower_priority on Linux).
BACKGROUND = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS} if IS_WINDOWS
              else {})


def lower_priority(pid: int) -> None:
    if not IS_WINDOWS:
        try:
            os.setpriority(os.PRIO_PROCESS, pid, 10)
        except OSError:
            pass

# Per-platform CS2 layout, relative to the "Counter-Strike Global Offensive" folder.
if IS_WINDOWS:
    GAME_BIN = "win64"
    CS2_LAUNCHER = "game/bin/win64/cs2.exe"
    PLUGIN_LIB = "server.dll"
else:
    GAME_BIN = "linuxsteamrt64"
    CS2_LAUNCHER = "game/cs2.sh"
    PLUGIN_LIB = "libserver.so"


def default_work_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "cs2hl"
    if IS_MAC:
        return Path.home() / "Library/Application Support/cs2hl"
    return Path.home() / ".local/share/cs2hl"


def _windows_steam_root() -> Path:
    try:
        import winreg
        for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    return Path(winreg.QueryValueEx(k, value)[0])
            except OSError:
                continue
    except ImportError:
        pass
    return Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"


def steam_root() -> Path:
    if env := os.environ.get("CS2HL_STEAM_DIR"):
        return Path(env)
    if IS_WINDOWS:
        return _windows_steam_root()
    if IS_MAC:
        return Path.home() / "Library/Application Support/Steam"
    candidates = [Path.home() / ".local/share/Steam", Path.home() / ".steam/steam",
                  Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]  # native, Debian, Flatpak
    return next((c for c in candidates if (c / "config/loginusers.vdf").exists()), candidates[0])


# --- processes ----------------------------------------------------------------------------------------

def process_running(name: str) -> bool:
    """Is a process with exactly this name (no .exe) running?"""
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"], capture_output=True,
                             text=True, **NO_WINDOW).stdout
        return f"{name}.exe".lower() in out.lower()
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


def kill_process(name: str, force: bool = False) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/IM", f"{name}.exe", *(["/F"] if force else [])], capture_output=True,
                       **NO_WINDOW)
    else:
        subprocess.run(["pkill", *(["-9"] if force else []), "-x", name], capture_output=True)


STEAM_PROCESS = "steam_osx" if IS_MAC else "steam"


def start_steam() -> None:
    """Start the Steam client minimised. Raises FileNotFoundError when Steam is not installed."""
    if IS_WINDOWS:
        exe = steam_root() / "steam.exe"
        if not exe.exists():
            raise FileNotFoundError(exe)
        subprocess.Popen([str(exe), "-silent"], **NO_WINDOW)
    elif IS_MAC:
        subprocess.Popen(["open", "-a", "Steam", "--args", "-silent"])
    else:
        subprocess.Popen(["steam", "-silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def open_target(target: str) -> None:
    """Open a file, folder or URL with the desktop's default program."""
    if IS_WINDOWS:
        os.startfile(target)  # noqa: S606 - only our own paths and allow-listed URLs reach this
    elif IS_MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


# --- folder links -----------------------------------------------------------------------------------

def is_link(path: Path) -> bool:
    """Symlink, or on Windows also a directory junction."""
    return path.is_symlink() or (IS_WINDOWS and hasattr(path, "is_junction") and path.is_junction())


def link_dir(link: Path, target: Path) -> None:
    """Make link point at the target folder. Windows uses a junction, which needs no admin rights."""
    if IS_WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True,
                       **NO_WINDOW)
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path) -> None:
    """Remove a symlink or junction without touching what it points to."""
    if IS_WINDOWS and not link.is_symlink():
        os.rmdir(link)  # a junction is removed like an empty folder; its target stays
    else:
        link.unlink()
