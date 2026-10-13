"""Settings and auto-discovery of Steam / CS2 / workspace paths."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STEAM_ROOT = Path.home() / ".local/share/Steam"
# Workspace lives on a big disk; this symlink has no spaces so CS2's +playdemo works.
WORK_DIR = Path.home() / ".local/share/cs2hl"
CONFIG_PATH = WORK_DIR / "config.toml"
CLIPS_DIR = WORK_DIR / "clips"
MONTAGES_DIR = WORK_DIR / "montages"
PLUGIN_SO = WORK_DIR / "bin/libserver.so"
STATE_PATH = WORK_DIR / "state.json"

# render_fps: frames CS2 renders per second of game time (frame-by-frame, not real time).
# blend: how many rendered frames are averaged into one output frame (motion blur).
MODES = {
    "smooth120": {"render_fps": 120, "blend": 1},
    "blur60": {"render_fps": 240, "blend": 4},
}


@dataclass
class Config:
    steamids: list[str] = field(default_factory=list)
    cs2_dir: Path | None = None
    mode: str = "smooth120"
    width: int = 1920
    height: int = 1080
    # "donk" (default), "mine" (your own crosshair), "player" (whatever the demo shows),
    # or a table of crosshair convars, e.g. {cl_crosshair_length = 3, cl_crosshair_gap = 0}
    crosshair: str | dict = "donk"
    # Highlight rules
    min_kills: int = 3
    clutch_min_enemies: int = 2
    pre_seconds: float = 3.0
    post_seconds: float = 2.0
    max_gap_seconds: float = 8.0
    # Give up on a CS2 launch after this many seconds (plus render time)
    launch_timeout: int = 300
    # Encoder
    video_codec: str = "h264_nvenc"
    quality: int = 23  # NVENC CQ / x264 CRF; lower = bigger, sharper

    @property
    def replays_dir(self) -> Path:
        return self.cs2_dir / "game/csgo/replays"

    @property
    def cs2_sh(self) -> Path:
        return self.cs2_dir / "game/cs2.sh"

    @property
    def mode_spec(self) -> dict:
        return MODES[self.mode]


def steam_accounts() -> list[str]:
    """SteamID64s of every account that has logged in on this machine."""
    vdf = STEAM_ROOT / "config/loginusers.vdf"
    if not vdf.exists():
        return []
    return re.findall(r'"(7656\d{13})"', vdf.read_text(errors="ignore"))


def find_cs2_dir() -> Path | None:
    """CS2 install that Steam updated most recently (stale copies may exist)."""
    vdf = STEAM_ROOT / "steamapps/libraryfolders.vdf"
    if not vdf.exists():
        return None
    best, best_time = None, -1
    for lib in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore")):
        manifest = Path(lib) / "steamapps/appmanifest_730.acf"
        game = Path(lib) / "steamapps/common/Counter-Strike Global Offensive"
        if not manifest.exists() or not (game / "game/cs2.sh").exists():
            continue
        m = re.search(r'"LastUpdated"\s+"(\d+)"', manifest.read_text(errors="ignore"))
        updated = int(m.group(1)) if m else 0
        if updated > best_time:
            best, best_time = game, updated
    return best


def load() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        data = tomllib.loads(CONFIG_PATH.read_text())
        for key, value in data.items():
            if not hasattr(cfg, key):
                raise SystemExit(f"Unknown config key in {CONFIG_PATH}: {key}")
            setattr(cfg, key, Path(value) if key == "cs2_dir" else value)
    if not cfg.steamids:
        cfg.steamids = steam_accounts()
    if cfg.cs2_dir is None:
        cfg.cs2_dir = find_cs2_dir()
    if cfg.cs2_dir is None:
        raise SystemExit("CS2 install not found; set cs2_dir in " + str(CONFIG_PATH))
    if cfg.mode not in MODES:
        raise SystemExit(f"mode must be one of {list(MODES)}")
    return cfg
