"""Settings, persisted as TOML, plus auto-discovery of Steam / CS2 paths."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .errors import FriendlyError

STEAM_ROOT = Path.home() / ".local/share/Steam"
# Workspace (venv, plugin, temporary frames, state). setup can point this symlink at a bigger disk;
# the path itself has no spaces so CS2's +playdemo works.
WORK_DIR = Path(os.environ.get("CS2HL_WORK_DIR", Path.home() / ".local/share/cs2hl"))
CONFIG_PATH = WORK_DIR / "config.toml"
PLUGIN_SO = WORK_DIR / "bin/libserver.so"
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
CODECS = ("h264_nvenc", "libx264")

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
    width: int = 1920
    height: int = 1080
    crosshair: str = "donk"
    custom_crosshair: dict = field(default_factory=lambda: dict(DEFAULT_CUSTOM_CROSSHAIR))
    # Highlight rules
    min_kills: int = 3
    clutch_min_enemies: int = 2
    pre_seconds: float = 3.0
    post_seconds: float = 2.0
    max_gap_seconds: float = 8.0
    # Delete demos downloaded from Steam once their highlights are rendered
    keep_downloaded_demos: bool = False
    # Encoder
    video_codec: str = "h264_nvenc"
    quality: int = 23  # NVENC CQ / x264 CRF; lower = bigger, sharper
    # Give up on a CS2 launch after this many seconds (plus expected render time)
    launch_timeout: int = 300

    # --- resolved paths -------------------------------------------------------------------------

    @property
    def cs2_path(self) -> Path:
        path = Path(self.cs2_dir).expanduser() if self.cs2_dir else find_cs2_dir()
        if path is None or not (path / "game/cs2.sh").exists():
            raise FriendlyError(
                "Counter-Strike 2 not found",
                f"No CS2 install at {path}." if path else "Steam's library list has no Linux CS2 install.",
                "Install CS2 through Steam, or pick the right folder under Settings → Counter-Strike folder "
                "(the folder that contains 'game/cs2.sh').")
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
        manifest = Path(lib) / "steamapps/appmanifest_730.acf"
        game = Path(lib) / "steamapps/common/Counter-Strike Global Offensive"
        if not manifest.exists() or not (game / "game/cs2.sh").exists():
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
