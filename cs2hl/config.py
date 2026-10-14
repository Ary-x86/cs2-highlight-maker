"""Settings, persisted as TOML, plus auto-discovery of Steam / CS2 paths."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import system
from .errors import FriendlyError

STEAM_ROOT = system.steam_root()
# Workspace (venv, plugin, temporary frames, state). On Linux, setup can point this symlink at a bigger disk;
# the path itself has no spaces so CS2's +playdemo works.
WORK_DIR = Path(os.environ.get("CS2HL_WORK_DIR", system.default_work_dir()))
CONFIG_PATH = WORK_DIR / "config.toml"
PLUGIN_SO = WORK_DIR / "bin" / system.PLUGIN_LIB
STATE_PATH = WORK_DIR / "state.json"
SESSION_PATH = WORK_DIR / "steam_session.json"
DOWNLOADS_DIR = WORK_DIR / "downloads"

# render_fps: frames CS2 renders per second of game time (frame-by-frame, not real time).
# blend: how many rendered frames are averaged into one output frame (motion blur).
MODES = {
    "smooth120": {"render_fps": 120, "blend": 1},
    "blur60": {"render_fps": 240, "blend": 4},
}
CROSSHAIR_CHOICES = ("donk", "classic", "custom")
# auto: NVIDIA NVENC if there is an NVIDIA GPU, else VAAPI on Linux / AMF or Quick Sync on Windows (AMD/Intel),
# else CPU. Falls back to CPU on errors.
CODECS = ("auto", "h264_nvenc", "h264_vaapi", "h264_amf", "h264_qsv", "libx264")
GRAPHICS = ("normal", "high")  # high: Valve's Very High preset while recording, your own settings afterwards
RESOLUTIONS = ((1920, 1080), (2560, 1440))
# Rounds worth a clip. "ace" means killing the whole enemy team, so it adapts to Wingman/Rush team sizes.
ROUND_TYPES = ("3k", "4k", "ace", "clutch")
# Single kills worth a clip on their own (see detect.SPECIAL_RULES).
SPECIAL_KILLS = ("flick", "sniper_flick", "spray_transfer", "long_range", "noscope", "wallbang", "smoke",
                 "jumpshot", "blind")

DEFAULT_CUSTOM_CROSSHAIR = {
    "cl_crosshairstyle": 4,
    "cl_crosshairdot": 1,
    "cl_crosshair_t": 0,
    "cl_crosshair_length": 4,
    "cl_crosshair_gap": 2,
    "cl_crosshair_thickness": 2,
    "cl_crosshair_drawoutline": 1,
    "cl_crosshaircolor_r": 0,
    "cl_crosshaircolor_g": 255,
    "cl_crosshaircolor_b": 0,
    "cl_crosshaircolor_a": 255,
}


@dataclass
class Config:
    # Empty means auto-detect (newest CS2 install / every account that logged in on this PC)
    cs2_dir: str = ""
    steamids: list[str] = field(default_factory=list)
    # Where clips/ and montages/ are written; empty means the workspace folder
    output_dir: str = ""
    mode: str = "smooth120"
    # Size CS2 renders at; output_height scales the final video (0 = same as recorded)
    width: int = 1920
    height: int = 1080
    output_height: int = 0
    graphics: str = "normal"
    crosshair: str = "donk"
    custom_crosshair: dict = field(default_factory=lambda: dict(DEFAULT_CUSTOM_CROSSHAIR))
    # Highlight rules
    round_highlights: list[str] = field(default_factory=lambda: ["3k", "4k", "ace", "clutch"])
    special_kills: list[str] = field(default_factory=list)
    clutch_min_enemies: int = 2
    pre_seconds: float = 3.0
    post_seconds: float = 2.0
    max_gap_seconds: float = 8.0
    # Delete demos downloaded from Steam once their highlights are rendered
    keep_downloaded_demos: bool = False
    # Encoder
    video_codec: str = "auto"
    quality: int = 23  # NVENC CQ / x264 CRF; lower = bigger, sharper
    # Give up on a CS2 launch after this many seconds (plus expected render time)
    launch_timeout: int = 300

    # --- resolved paths -------------------------------------------------------------------------

    @property
    def cs2_path(self) -> Path:
        if system.IS_MAC:
            raise FriendlyError(
                "CS2 does not run on macOS",
                "Valve only makes Counter-Strike 2 for Windows and Linux, and recording happens inside the game.",
                "Use this app on a Windows or Linux PC. On a Mac you can still list the highlights in a demo.")
        path = Path(self.cs2_dir).expanduser() if self.cs2_dir else find_cs2_dir()
        if path is None or not (path / system.CS2_LAUNCHER).exists():
            raise FriendlyError(
                "Counter-Strike 2 not found",
                f"No CS2 install at {path}." if path else "Steam's library list has no CS2 install.",
                "Install CS2 through Steam, or pick the right folder under Settings → Counter-Strike folder "
                f"(the 'Counter-Strike Global Offensive' folder that contains '{system.CS2_LAUNCHER}').")
        return path

    @property
    def replays_dir(self) -> Path:
        return self.cs2_path / "game/csgo/replays"

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser() if self.output_dir else WORK_DIR

    @property
    def clips_dir(self) -> Path:
        return self.output_path / "clips"

    @property
    def montages_dir(self) -> Path:
        return self.output_path / "montages"

    @property
    def accounts(self) -> list[str]:
        return self.steamids or steam_accounts()

    @property
    def mode_spec(self) -> dict:
        return MODES[self.mode]

    @property
    def final_height(self) -> int:
        return self.output_height if 0 < self.output_height < self.height else self.height

    def validate(self) -> None:
        if self.mode not in MODES:
            raise FriendlyError("Unknown video mode", f"'{self.mode}' is not a mode.",
                                f"Use one of: {', '.join(MODES)}.")
        if self.crosshair not in CROSSHAIR_CHOICES:
            raise FriendlyError("Unknown crosshair", f"'{self.crosshair}' is not a choice.",
                                f"Use one of: {', '.join(CROSSHAIR_CHOICES)}.")
        if self.video_codec not in CODECS:
            raise FriendlyError("Unknown encoder", f"'{self.video_codec}' is not supported.",
                                f"Use one of: {', '.join(CODECS)}.")
        if self.graphics not in GRAPHICS:
            raise FriendlyError("Unknown graphics setting", f"'{self.graphics}' is not a choice.",
                                f"Use one of: {', '.join(GRAPHICS)}.")
        for name, value, known in (("highlight type", self.round_highlights, ROUND_TYPES),
                                   ("special kill", self.special_kills, SPECIAL_KILLS)):
            if unknown := [v for v in value if v not in known]:
                raise FriendlyError(f"Unknown {name}", ", ".join(unknown), f"Use any of: {', '.join(known)}.")

    def to_dict(self) -> dict:
        return asdict(self)


def steam_accounts() -> list[str]:
    """SteamID64s of every account that has logged in on this machine."""
    vdf = STEAM_ROOT / "config/loginusers.vdf"
    if not vdf.exists():
        return []
    return re.findall(r'"(7656\d{13})"', vdf.read_text(errors="ignore"))


def find_cs2_installs() -> list[dict]:
    """Every CS2 install Steam knows about, most recently updated first."""
    vdf = STEAM_ROOT / "steamapps/libraryfolders.vdf"
    if not vdf.exists():
        return []
    installs = []
    for lib in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore")):
        lib = lib.replace("\\\\", "\\")  # Windows paths are stored with escaped backslashes
        manifest = Path(lib) / "steamapps/appmanifest_730.acf"
        game = Path(lib) / "steamapps/common/Counter-Strike Global Offensive"
        if not manifest.exists() or not (game / system.CS2_LAUNCHER).exists():
            continue
        m = re.search(r'"LastUpdated"\s+"(\d+)"', manifest.read_text(errors="ignore"))
        installs.append({"path": str(game), "updated": int(m.group(1)) if m else 0})
    return sorted(installs, key=lambda i: i["updated"], reverse=True)


def find_cs2_dir() -> Path | None:
    """CS2 install that Steam updated most recently (stale copies may exist)."""
    installs = find_cs2_installs()
    return Path(installs[0]["path"]) if installs else None


def load() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CONFIG_PATH.read_text())
        except tomllib.TOMLDecodeError as e:
            raise FriendlyError("Settings file is broken", f"{CONFIG_PATH}: {e}",
                                "Fix the line mentioned, or delete the file to go back to defaults.")
        known = {f.name for f in fields(Config)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
    if (cfg.width, cfg.height) not in RESOLUTIONS:  # older versions offered 720p
        cfg.width, cfg.height = RESOLUTIONS[0]
    cfg.validate()
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {value!r} to TOML")


def save(cfg: Config) -> None:
    cfg.validate()
    lines, tables = [], []
    for key, value in cfg.to_dict().items():
        if isinstance(value, dict):
            tables.append(f"\n[{key}]")
            tables += [f"{k} = {_toml_value(v)}" for k, v in value.items()]
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines + tables) + "\n")
    tmp.replace(CONFIG_PATH)
