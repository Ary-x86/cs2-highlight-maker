"""Temporarily change CS2's video settings for a recording, then put the player's own file back.

CS2 keeps display mode, resolution and graphics quality per Steam account in userdata/<id>/730/local/cfg/cs2_video.txt
and rewrites that file when it quits, so launch options used for recording (windowed, a fixed size) and any quality
boost would otherwise stick. The original file is copied aside before launch and copied back once CS2 has exited.
A marker file makes sure a crashed run is undone the next time.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

VIDEO_FILE = "cs2_video.txt"

# Valve's own "Very High" preset (csgo/cfg/video_defaults_3.txt inside the game files), minus FSR upscaling.
HIGH_QUALITY = {
    "setting.shaderquality": "1",
    "setting.r_texturefilteringquality": "5",   # anisotropic 16x
    "setting.msaa_samples": "8",
    "setting.r_csgo_cmaa_enable": "0",
    "setting.videocfg_shadow_quality": "3",
    "setting.videocfg_dynamic_shadows": "1",
    "setting.videocfg_texture_detail": "2",
    "setting.videocfg_particle_detail": "3",
    "setting.videocfg_ao_detail": "3",
    "setting.videocfg_hdr_detail": "-1",
    "setting.videocfg_fsr_detail": "0",          # FidelityFX Super Resolution off
}

# Used only when the account had no cs2_video.txt before recording: fullscreen windowed, 16:9, 1920x1080.
FALLBACK_DISPLAY = {
    "setting.fullscreen": "1",
    "setting.coop_fullscreen": "1",
    "setting.nowindowborder": "1",
    "setting.aspectratiomode": "1",
    "setting.defaultres": "1920",
    "setting.defaultresheight": "1080",
}

_LINE = re.compile(r'^(\s*)"([^"]+)"(\s+)"([^"]*)"\s*$')


def read(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(errors="ignore").splitlines():
        if m := _LINE.match(line):
            values[m.group(2)] = m.group(4)
    return values


def write_values(path: Path, changes: dict[str, str]) -> None:
    """Set keys in a cs2_video.txt, keeping every other line as it is."""
    lines = path.read_text(errors="ignore").splitlines() if path.exists() else ['"video.cfg"', "{", "}"]
    todo = dict(changes)
    for i, line in enumerate(lines):
        if (m := _LINE.match(line)) and m.group(2) in todo:
            lines[i] = f'{m.group(1)}"{m.group(2)}"{m.group(3)}"{todo.pop(m.group(2))}"'
    if todo:
        close = max(i for i, line in enumerate(lines) if line.strip() == "}")
        lines[close:close] = [f'\t"{k}"\t\t"{v}"' for k, v in todo.items()]
    path.write_text("\n".join(lines) + "\n")


class VideoSettings:
    """Backup/restore of one account's cs2_video.txt around a recording."""

    def __init__(self, cfg_dir: Path | None, backup_dir: Path):
        self.target = cfg_dir / VIDEO_FILE if cfg_dir else None
        self.backup_dir = backup_dir
        self.copy = backup_dir / VIDEO_FILE
        self.marker = backup_dir / "pending.json"

    def apply(self, changes: dict[str, str]) -> None:
        if self.target is None:
            return
        self.restore()  # undo a crashed earlier run first
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        existed = self.target.exists()
        if existed:
            shutil.copy2(self.target, self.copy)
        self.marker.write_text(json.dumps({"target": str(self.target), "existed": existed}))
        if changes:
            write_values(self.target, changes)

    def restore(self) -> None:
        """Put the player's file back. Safe to call more than once, and after a crash."""
        try:
            info = json.loads(self.marker.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        target = Path(info["target"])
        if info.get("existed") and self.copy.exists():
            shutil.copy2(self.copy, target)
        elif target.exists():
            # No file before recording: CS2 created one from the recording session. Give it sane display settings.
            write_values(target, FALLBACK_DISPLAY)
        self.copy.unlink(missing_ok=True)
        self.marker.unlink()
