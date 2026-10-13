"""Drive CS2 demo playback + frame-by-frame recording through the CS:DM server plugin.

The plugin (patched build, see plugin/) is loaded by CS2 via an extra gameinfo.gi search path. At startup it reads
`<demo>.json`, a list of sequences of {tick, cmd} actions, and runs each command when playback reaches its tick.
`startmovie` with `host_framerate` makes the engine render every frame at a fixed rate regardless of real-time speed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import usercfg
from .config import STEAM_ROOT

SETUP_TICK = 96  # first tick where the plugin runs actions after a seek to tick 0
SESSION_COMMANDS = (
    "sv_cheats 1",
    "volume 1",
    "cl_hud_telemetry_frametime_show 0",
    "cl_hud_telemetry_net_misdelivery_show 0",
    "cl_hud_telemetry_ping_show 0",
    "cl_hud_telemetry_serverrecvmargin_graph_show 0",
    "cl_trueview_show_status 0",
    "r_show_build_info 0",
    "cl_demo_predict 0",
    "cl_draw_only_deathnotices 1",
    "tv_listen_voice_indices 0",
    "tv_listen_voice_indices_h 0",
    "jpeg_quality 100",
)
# Convars set during recording besides SESSION_COMMANDS (crosshair ones are matched by prefix).
EXTRA_TOUCHED = ("spec_show_xray", "mp_display_kill_assists", "cl_show_observer_crosshair")
# Crosshair presets in CS2's pixel-based crosshair convars (apply_crosshair_code no longer changes them).
CROSSHAIRS = {
    # donk (Team Spirit), August 2026: classic static, size 1, gap -4.5, thickness 1, cyan, no outline,
    # converted to pixels at 1080p (legacy units scale by 1080/480).
    "donk": {
        "cl_crosshairstyle": 4,
        "cl_crosshairdot": 0,
        "cl_crosshair_t": 0,
        "cl_crosshair_length": 2,
        "cl_crosshair_gap": 1,
        "cl_crosshair_thickness": 2,
        "cl_crosshair_drawoutline": 0,
        "cl_crosshaircolor_r": 0,
        "cl_crosshaircolor_g": 255,
        "cl_crosshaircolor_b": 255,
        "cl_crosshaircolor_a": 255,
        "cl_crosshair_recoil": 0,
    },
}
GAMEINFO_SEARCH_PATH = "Game\tcsgo"


@dataclass
class Clip:
    name: str  # unique, filesystem-safe; also the startmovie prefix
    start_tick: int
    end_tick: int
    player_slot: int  # user_id + 1 of the player to spectate


class Cs2:
    def __init__(self, cs2_dir: Path, plugin_so: Path, raw_dir: Path):
        self.cs2_dir = cs2_dir
        self.csgo = cs2_dir / "game/csgo"
        self.plugin_dir = self.csgo / "csdm"
        self.gameinfo = self.csgo / "gameinfo.gi"
        self.plugin_so = plugin_so
        self.raw_dir = raw_dir
        # frames land in csgo/csdm/movie, audio in csgo/movie; both are redirected to raw_dir
        self.frames_link = self.plugin_dir / "movie"
        self.audio_link = self.csgo / "movie"

    @staticmethod
    def is_running() -> bool:
        return subprocess.run(["pgrep", "-x", "cs2"], capture_output=True).returncode == 0

    # --- plugin install -------------------------------------------------------------------------

    def install(self) -> None:
        self.uninstall()
        bin_dir = self.plugin_dir / "bin/linuxsteamrt64"
        bin_dir.mkdir(parents=True)
        shutil.copy2(self.plugin_so, bin_dir / "libserver.so")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.frames_link.symlink_to(self.raw_dir, target_is_directory=True)
        if not self.audio_link.exists() and not self.audio_link.is_symlink():
            self.audio_link.symlink_to(self.raw_dir, target_is_directory=True)

        content = self.gameinfo.read_text()
        if "Game\tcsgo/csdm" not in content:
            # The plugin restores gameinfo.gi from this backup as soon as it is loaded.
            shutil.copy2(self.gameinfo, self.gameinfo.with_name("gameinfo.gi.backup"))
            content = content.replace(GAMEINFO_SEARCH_PATH, "Game\tcsgo/csdm\n\t\t\t" + GAMEINFO_SEARCH_PATH, 1)
            self.gameinfo.write_text(content)

    def uninstall(self) -> None:
        for link in (self.frames_link, self.audio_link):
            if link.is_symlink():
                link.unlink()
        if self.plugin_dir.exists():
            shutil.rmtree(self.plugin_dir)
        backup = self.gameinfo.with_name("gameinfo.gi.backup")
        if backup.exists():
            shutil.copy2(backup, self.gameinfo)
            backup.unlink()

    # --- actions file ---------------------------------------------------------------------------

    @staticmethod
    def crosshair_commands(crosshair: str | dict) -> list[str]:
        """"player": whatever the demo shows; "mine": your local crosshair; a preset name from CROSSHAIRS;
        a {cvar: value} table; or a CSGO-... share code (best effort)."""
        if crosshair == "player":
            return []
        cmds = ["cl_show_observer_crosshair 0"]  # draw the local crosshair instead of the spectated one
        if crosshair == "mine":
            return cmds
        cvars = CROSSHAIRS.get(crosshair, crosshair) if isinstance(crosshair, str) else crosshair
        if isinstance(cvars, dict):
            cmds += [f"{name} {value}" for name, value in cvars.items()]
        elif cvars.startswith("CSGO-"):
            cmds.append(f"apply_crosshair_code {cvars}")
        else:
            raise ValueError(f"unknown crosshair {crosshair!r}; presets: {', '.join(CROSSHAIRS)}")
        return cmds

    @staticmethod
    def actions(clips: list[Clip], framerate: int, crosshair: str | dict = "donk", restore: list[str] = (),
                tickrate: int = 64) -> list[dict]:
        sequences = []
        for i, clip in enumerate(clips):
            setup = max(SETUP_TICK + 1, clip.start_tick - tickrate)
            acts = [(SETUP_TICK, c) for c in (
                *SESSION_COMMANDS,
                *Cs2.crosshair_commands(crosshair),
                # Seek to just before the setup tick; seeking and spec_player on the same tick is unreliable.
                f"demo_gototick {setup - 1}",
            )]
            acts += [(setup, c) for c in (
                "spec_show_xray 0",
                "mp_display_kill_assists 1",
                f"host_framerate {framerate}",
            )]
            # Pause briefly before recording so the seek's loading tint is gone.
            acts.append((max(setup + 1, clip.start_tick - 4), "pause_playback"))
            acts += [(clip.start_tick, c) for c in (
                "spec_mode 1",
                f"spec_player {clip.player_slot}",
                f"startmovie {clip.name} jpg wav",
            )]
            acts.append((clip.end_tick, "endmovie"))
            if i == len(clips) - 1:
                # CS2 saves archived convars on exit: put the player's own values back first.
                acts += [(clip.end_tick + 1, c) for c in ("host_framerate 0", *restore)]
                acts.append((clip.end_tick + 2, "quit"))
            else:
                acts.append((clip.end_tick + tickrate, "go_to_next_sequence"))
            sequences.append({"actions": [{"tick": t, "cmd": c} for t, c in acts]})
        return sequences

    # --- launch ---------------------------------------------------------------------------------

    def _runtime_entry(self) -> Path:
        for lib in (STEAM_ROOT, self.cs2_dir.parent.parent.parent):
            entry = lib / "steamapps/common/SteamLinuxRuntime_sniper/_v2-entry-point"
            if entry.exists():
                return entry
        raise SystemExit("SteamLinuxRuntime_sniper not found; install it from Steam (Tools)")

    def record(self, demo: Path, clips: list[Clip], framerate: int, width: int, height: int,
               timeout: float, crosshair: str | dict = "donk", log=print) -> None:
        """Play demo, record every clip, return once the game has quit."""
        if " " in str(demo):
            raise ValueError("demo path must not contain spaces (CS2 splits +playdemo on them)")
        cfg_dir = usercfg.active_account_cfg_dir()
        saved = usercfg.read_convars(cfg_dir)
        touched = [c.split()[0] for c in SESSION_COMMANDS] + list(EXTRA_TOUCHED)
        touched += [k for k in saved if k.startswith("cl_crosshair")]
        restore = usercfg.restore_commands(saved, touched)
        cfg_backup = self.raw_dir / "cfg-backup"
        usercfg.backup(cfg_dir, cfg_backup)

        actions_file = Path(str(demo) + ".json")
        actions_file.write_text(json.dumps(self.actions(clips, framerate, crosshair, restore), indent=1))
        self.install()
        cmd = [str(self._runtime_entry()), "--verb=waitforexitandrun", "--",
               str(self.cs2_dir / "game/cs2.sh"), "-insecure", "-novid", "-nojoy",
               "+playdemo", str(demo), "-width", str(width), "-height", str(height), "-sw"]
        env = dict(os.environ, SteamAppId="730", SteamGameId="730")
        log(f"launching CS2 for {len(clips)} clip(s)")
        # CS2 prints its console to stdout on Linux; keep it next to the frames for debugging.
        console = open(self.console_log(), "w")
        proc = subprocess.Popen(cmd, env=env, stdout=console, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("CS2 did not finish recording in time")
                time.sleep(2)
            # cs2.sh may exit before the game process itself is gone
            while self.is_running() and time.monotonic() < deadline:
                time.sleep(1)
        except BaseException:
            self.kill()
            usercfg.restore_backup(cfg_dir, cfg_backup)
            raise
        finally:
            console.close()
            self.uninstall()
            actions_file.unlink(missing_ok=True)

    @staticmethod
    def kill() -> None:
        subprocess.run(["pkill", "-x", "cs2"], capture_output=True)
        time.sleep(3)
        subprocess.run(["pkill", "-9", "-x", "cs2"], capture_output=True)

    def console_log(self) -> Path:
        return self.raw_dir.parent / "cs2-console.log"

    def plugin_log(self) -> Path:
        return self.cs2_dir / "game/bin/linuxsteamrt64/csdm.log"
