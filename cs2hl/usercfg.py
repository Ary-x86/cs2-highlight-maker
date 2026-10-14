"""Read the active Steam account's saved CS2 convars so recording tweaks can be undone before CS2 quits.

CS2 writes archived convars (volume, crosshair, HUD toggles...) back to these files on exit, so anything the
recording session changes would otherwise stick in the player's real config.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import STEAM_ROOT

STEAMID64_BASE = 76561197960265728
CFG_FILES = ("cs2_user_convars_0_slot0.vcfg", "cs2_machine_convars.vcfg")


def active_account_cfg_dir() -> Path | None:
    """userdata/<accountid>/730/local/cfg of the account Steam logged in most recently."""
    vdf = STEAM_ROOT / "config/loginusers.vdf"
    if not vdf.exists():
        return None
    text = vdf.read_text(encoding="utf-8", errors="ignore")
    best, best_time = None, -1
    for steamid, body in re.findall(r'"(7656\d{13})"\s*\{([^}]*)\}', text):
        # Key case varies between Steam versions ("MostRecent"/"Timestamp" vs "timestamp").
        if re.search(r'"MostRecent"\s+"1"', body, re.I):
            best_time, best = float("inf"), steamid
        m = re.search(r'"Timestamp"\s+"(\d+)"', body, re.I)
        if m and int(m.group(1)) > best_time:
            best, best_time = steamid, int(m.group(1))
    if best is None:
        return None
    return STEAM_ROOT / "userdata" / str(int(best) - STEAMID64_BASE) / "730/local/cfg"


def read_convars(cfg_dir: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if cfg_dir is None:
        return values
    for name in CFG_FILES:
        path = cfg_dir / name
        if path.exists():
            values.update(re.findall(r'^\s*"([^"]+)"\s+"([^"]*)"\s*$', path.read_text(encoding="utf-8", errors="ignore"), re.M))
    return values


def restore_commands(saved: dict[str, str], touched: list[str]) -> list[str]:
    """Console commands that put every touched convar back to its saved value."""
    cmds = []
    for name in touched:
        if name not in saved:
            continue
        value = {"true": "1", "false": "0"}.get(saved[name], saved[name])
        cmds.append(f'{name} "{value}"')
    return cmds


def backup(cfg_dir: Path | None, dest: Path) -> None:
    if cfg_dir is None:
        return
    dest.mkdir(parents=True, exist_ok=True)
    for path in cfg_dir.glob("*.vcfg*"):
        shutil.copy2(path, dest / path.name)


def restore_backup(cfg_dir: Path | None, src: Path) -> None:
    """Fallback for when CS2 had to be killed before it could restore its own settings."""
    if cfg_dir is None or not src.exists():
        return
    for path in src.glob("*.vcfg*"):
        shutil.copy2(path, cfg_dir / path.name)
