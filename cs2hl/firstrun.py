"""First-run checks and the fixes the setup screen can do by itself.

The packaged app (Windows .exe / Linux binary) carries a prebuilt recording plugin and unpacks it into the
workspace. On Windows FFmpeg is usually missing, so the setup screen can download it into the workspace too.
"""

from __future__ import annotations

import filecmp
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from . import encode, system
from .config import CONFIG_PATH, PLUGIN_SO, WORK_DIR
from .errors import FriendlyError

# Static FFmpeg build for Windows with NVENC, AMF and Quick Sync, from the BtbN/FFmpeg-Builds project.
FFMPEG_WINDOWS_ZIP = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                      "ffmpeg-master-latest-win64-gpl.zip")


def bundled_plugin() -> Path:
    return system.BUNDLE_DIR / "plugin" / system.PLUGIN_LIB


def provision_plugin() -> None:
    """Copy the plugin shipped inside the packaged app into the workspace (new install or app update)."""
    src = bundled_plugin()
    if not system.FROZEN or not src.exists():
        return  # running from source: install.sh / plugin/build.sh builds it
    if PLUGIN_SO.exists() and filecmp.cmp(src, PLUGIN_SO, shallow=False):
        return
    PLUGIN_SO.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, PLUGIN_SO)


def needed() -> bool:
    """The setup screen opens by itself until it has been finished once (that writes the settings file)."""
    return not CONFIG_PATH.exists()


def platform_name() -> str:
    return "windows" if system.IS_WINDOWS else "mac" if system.IS_MAC else "linux"


class FfmpegDownload:
    def __init__(self):
        self.running = False
        self.progress = 0.0
        self.error: dict | None = None

    def status(self) -> dict:
        return {"running": self.running, "progress": self.progress, "error": self.error}

    def start(self) -> None:
        if not system.IS_WINDOWS:
            raise FriendlyError("Install FFmpeg with your package manager",
                                "Automatic download is only offered on Windows.",
                                "Linux: e.g. `sudo apt install ffmpeg` or `sudo pacman -S ffmpeg`.")
        if self.running:
            return
        self.running, self.progress, self.error = True, 0.0, None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        tmp = WORK_DIR / "tmp" / "ffmpeg.zip"
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(FFMPEG_WINDOWS_ZIP, headers={"User-Agent": "cs2-highlight-maker"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    got += len(chunk)
                    self.progress = got / total * 0.95 if total else 0.5
            dest = WORK_DIR / "bin" / "ffmpeg.exe"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp) as z:
                member = next(n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe"))
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            self.progress = 1.0
        except Exception as e:  # noqa: BLE001 - shown on the setup screen
            self.error = FriendlyError("FFmpeg download failed", f"{type(e).__name__}: {e}",
                                       "Check your internet connection and try again, or install it yourself "
                                       "with `winget install ffmpeg` and restart the app.").to_dict()
        finally:
            tmp.unlink(missing_ok=True)
            self.running = False


def checks() -> dict:
    return {"needed": needed(), "platform": platform_name(), "ffmpeg_ok": encode.ffmpeg_available(),
            "frozen": system.FROZEN}
