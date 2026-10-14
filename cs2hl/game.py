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

from . import system, usercfg
from .config import STEAM_ROOT, WORK_DIR
from .errors import FriendlyError
from .videocfg import VideoSettings

SETUP_TICK = 96  # first tick where the plugin runs actions after a seek to tick 0
GAMEINFO_SEARCH_PATH = "Game\tcsgo"
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
    },
    # White four lines with a centre dot and a black outline.
    "classic": {
        "cl_crosshairstyle": 2,
        "cl_crosshairdot": 1,
        "cl_crosshair_t": 0,
        "cl_crosshair_length": 8,
        "cl_crosshair_gap": 4,
        "cl_crosshair_thickness": 2,
        "cl_crosshair_drawoutline": 1,
        "cl_crosshaircolor_r": 255,
        "cl_crosshaircolor_g": 255,
        "cl_crosshaircolor_b": 255,
        "cl_crosshaircolor_a": 255,
    },
}
# Outline colour and recoil-follow are fixed: black outline, static crosshair.
CROSSHAIR_FIXED = {"cl_crosshairoutline_r": 0, "cl_crosshairoutline_g": 0, "cl_crosshairoutline_b": 0,
                   "cl_crosshairoutline_a": 255, "cl_crosshair_recoil": 0}

PLUGIN_HOOK_TIMEOUT = 240  # seconds to wait for the plugin to report it is driving playback


def video_settings(cfg_dir: Path | None = None) -> VideoSettings:
    if cfg_dir is None:
        cfg_dir = usercfg.active_account_cfg_dir()
    return VideoSettings(cfg_dir, WORK_DIR / "tmp/video-backup")


@dataclass
class Clip:
    name: str  # unique, filesystem-safe; also the startmovie prefix
    start_tick: int
    end_tick: int
    player_slot: int  # user_id + 1 of the player to spectate


def crosshair_cvars(crosshair: str, custom: dict | None = None) -> dict:
    cvars = dict(custom or {}) if crosshair == "custom" else dict(CROSSHAIRS[crosshair])
    cvars.update(CROSSHAIR_FIXED)
    return cvars


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
        return system.process_running("cs2")

    # --- plugin install -------------------------------------------------------------------------

    def install(self) -> None:
        if not self.plugin_so.exists():
            raise FriendlyError("Recording plugin is missing", f"{self.plugin_so} does not exist.",
                                "Run the setup again: ./install.sh on Linux (it builds the plugin), or restart "
                                "the Windows app, which unpacks it.")
        self.uninstall()
        bin_dir = self.plugin_dir / "bin" / system.GAME_BIN
        bin_dir.mkdir(parents=True)
        shutil.copy2(self.plugin_so, bin_dir / system.PLUGIN_LIB)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        system.link_dir(self.frames_link, self.raw_dir)
        if not self.audio_link.exists() and not system.is_link(self.audio_link):
            system.link_dir(self.audio_link, self.raw_dir)

        content = self.gameinfo.read_text(encoding="utf-8")
        if "Game\tcsgo/csdm" not in content:
            # The plugin restores gameinfo.gi from this backup as soon as it is loaded.
            shutil.copy2(self.gameinfo, self.gameinfo.with_name("gameinfo.gi.backup"))
            content = content.replace(GAMEINFO_SEARCH_PATH, "Game\tcsgo/csdm\n\t\t\t" + GAMEINFO_SEARCH_PATH, 1)
            self.gameinfo.write_text(content, encoding="utf-8")

    def uninstall(self) -> None:
        for link in (self.frames_link, self.audio_link):
            if system.is_link(link):
                system.remove_link(link)
        if self.plugin_dir.exists():
            shutil.rmtree(self.plugin_dir)
        backup = self.gameinfo.with_name("gameinfo.gi.backup")
        if backup.exists():
            shutil.copy2(backup, self.gameinfo)
            backup.unlink()

    # --- actions file ---------------------------------------------------------------------------

    @staticmethod
    def crosshair_commands(crosshair: str, custom: dict | None = None) -> list[str]:
        """Force the local crosshair (the spectated player's own one only keeps its colours) and set it."""
        cvars = crosshair_cvars(crosshair, custom)
        return ["cl_show_observer_crosshair 0"] + [f"{name} {int(value)}" for name, value in cvars.items()]

    @staticmethod
    def actions(clips: list[Clip], framerate: int, crosshair: str = "donk", custom_crosshair: dict | None = None,
                restore: list[str] = (), tickrate: int = 64) -> list[dict]:
        sequences = []
        for i, clip in enumerate(clips):
            setup = max(SETUP_TICK + 1, clip.start_tick - tickrate)
            acts = [(SETUP_TICK, c) for c in (
                *SESSION_COMMANDS,
                *Cs2.crosshair_commands(crosshair, custom_crosshair),
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

    def launch_command(self, demo: Path, width: int, height: int) -> list[str]:
        args = ["-insecure", "-novid", "-nojoy", "+playdemo", str(demo), "-width", str(width), "-height",
                str(height), "-sw"]
        if system.IS_WINDOWS:
            return [str(self.cs2_dir / system.CS2_LAUNCHER), *args]
        # On Linux CS2 runs inside Steam's container runtime, like Steam itself would start it.
        return [str(self._runtime_entry()), "--verb=waitforexitandrun", "--", str(self.cs2_dir / "game/cs2.sh"),
                *args]

    def _runtime_entry(self) -> Path:
        for lib in (STEAM_ROOT, self.cs2_dir.parent.parent.parent):
            entry = lib / "steamapps/common/SteamLinuxRuntime_sniper/_v2-entry-point"
            if entry.exists():
                return entry
        raise FriendlyError("Steam Linux Runtime (sniper) is missing", "CS2 runs inside it on Linux.",
                            "In Steam, open Library → Tools and install 'Steam Linux Runtime 3.0 (sniper)'.")

    def _frames_done(self) -> int:
        count = 0
        for take in self.raw_dir.iterdir() if self.raw_dir.exists() else ():
            if take.is_dir():
                count += sum(1 for e in os.scandir(take) if e.name.endswith(".jpg"))
        return count

    def _plugin_hooked(self) -> bool:
        try:
            return "Hooked FrameStageNotify" in self.plugin_log().read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            return False

    def record(self, demo: Path, clips: list[Clip], framerate: int, width: int, height: int,
               timeout: float, crosshair: str = "donk", custom_crosshair: dict | None = None,
               video_changes: dict | None = None, log=print, progress=None) -> None:
        """Play demo, record every clip, return once the game has quit.

        video_changes are cs2_video.txt settings used only for this session (e.g. higher graphics quality).
        progress(frames_done, frames_total) is called every couple of seconds while recording.
        """
        if " " in str(demo) and not system.IS_WINDOWS:
            raise ValueError("demo path must not contain spaces (CS2 splits +playdemo on them)")
        cfg_dir = usercfg.active_account_cfg_dir()
        saved = usercfg.read_convars(cfg_dir)
        touched = [c.split()[0] for c in SESSION_COMMANDS] + list(EXTRA_TOUCHED)
        touched += [k for k in saved if k.startswith("cl_crosshair")]
        restore = usercfg.restore_commands(saved, touched)
        cfg_backup = self.raw_dir.parent / "cfg-backup"
        usercfg.backup(cfg_dir, cfg_backup)
        video = video_settings(cfg_dir)

        tickrate = 64
        total = sum(round((c.end_tick - c.start_tick) / tickrate * framerate) for c in clips)
        actions_file = Path(str(demo) + ".json")
        actions_file.write_text(json.dumps(
            self.actions(clips, framerate, crosshair, custom_crosshair, restore), indent=1))
        self.install()
        video.apply(video_changes or {})
        self.plugin_log().unlink(missing_ok=True)
        cmd = self.launch_command(demo, width, height)
        env = dict(os.environ, SteamAppId="730", SteamGameId="730")
        log(f"Launching CS2 to record {len(clips)} take(s), {total} frames")
        # CS2 prints its early console output to stdout on Linux; keep it for debugging.
        console = open(self.console_log(), "w")
        # Windows: start in the game's bin folder, where the plugin writes csdm.log.
        cwd = {"cwd": str(Path(cmd[0]).parent)} if system.IS_WINDOWS else {}
        proc = subprocess.Popen(cmd, env=env, stdout=console, stderr=subprocess.STDOUT, **cwd)
        started = time.monotonic()
        try:
            deadline = started + timeout
            hooked = False
            while proc.poll() is None or self.is_running():
                now = time.monotonic()
                if now > deadline:
                    raise FriendlyError(
                        "CS2 took too long to record",
                        f"Recording did not finish within {timeout / 60:.0f} minutes.",
                        "Make sure nothing covers or minimises the CS2 window, then try again. "
                        "Slow disks also slow down recording.")
                if not hooked:
                    hooked = self._plugin_hooked()
                    if not hooked and now - started > PLUGIN_HOOK_TIMEOUT:
                        raise FriendlyError(
                            "The recording plugin did not start inside CS2",
                            "A CS2 update probably changed the game internals the plugin relies on.",
                            "Update this tool (Linux: git pull, then ./install.sh; Windows: download the newest "
                            "release). If it still fails, check "
                            "github.com/akiver/cs-demo-manager for a newer plugin.")
                if progress:
                    progress(self._frames_done(), total)
                time.sleep(2)
        except BaseException:
            self.kill()
            usercfg.restore_backup(cfg_dir, cfg_backup)
            raise
        finally:
            console.close()
            # CS2 has exited (or was killed) and written its settings: put the player's display/quality back.
            video.restore()
            self.uninstall()
            actions_file.unlink(missing_ok=True)
        if progress:
            progress(self._frames_done(), total)
        if self._frames_done() == 0:
            raise FriendlyError(
                "CS2 closed without recording anything",
                "The game quit or crashed before reaching the highlights.",
                "Make sure Steam is running and you can open CS2 normally, then try again. "
                f"Details: {self.plugin_log()}")

    @staticmethod
    def kill() -> None:
        system.kill_process("cs2")
        time.sleep(3)
        system.kill_process("cs2", force=True)

    def console_log(self) -> Path:
        return self.raw_dir.parent / "cs2-console.log"

    def plugin_log(self) -> Path:
        return self.cs2_dir / "game/bin" / system.GAME_BIN / "csdm.log"
