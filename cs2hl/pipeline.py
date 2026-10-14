"""Demo -> highlights -> recorded takes -> encoded clips + montage."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import detect, encode, usercfg, videocfg
from .config import MODES, PLUGIN_SO, STATE_PATH, WORK_DIR, Config
from .errors import FriendlyError
from .game import Clip, Cs2

AVG_JPG_BYTES = 250_000  # 1080p frame at jpeg_quality 100 is ~160KB; leave headroom


class Reporter:
    """Receives progress from a running job. The CLI prints; the GUI streams it to the browser."""

    def log(self, msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    def note(self, msg: str) -> None:
        """Like log(), but safe to call while unwinding (never raises)."""
        self.log(msg)

    def stage(self, name: str) -> None:
        self.log(f"== {name}")

    def progress(self, fraction: float | None, detail: str = "") -> None:
        pass


# --- state ------------------------------------------------------------------------------------------

def demo_key(demo: Path) -> str:
    return f"{demo.name}:{demo.stat().st_size}"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def mark(key: str, **info) -> None:
    state = load_state()
    state[key] = {**info, "at": datetime.now().isoformat(timespec="seconds")}
    save_state(state)


def pending_demos(cfg: Config) -> list[Path]:
    state = load_state()
    demos = sorted(cfg.replays_dir.glob("*.dem"), key=lambda p: p.stat().st_mtime)
    return [d for d in demos if demo_key(d) not in state]


# --- helpers ----------------------------------------------------------------------------------------

def steam_running() -> bool:
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0


def ensure_steam(rep: Reporter) -> None:
    if steam_running():
        return
    rep.log("Steam is not running, starting it")
    try:
        subprocess.Popen(["steam", "-silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except FileNotFoundError:
        raise FriendlyError("Steam is not running", "CS2 needs the Steam client to start.",
                            "Start Steam, log in, then try again.")
    for _ in range(60):
        time.sleep(1)
        if steam_running():
            time.sleep(20)  # let it finish logging in
            return
    raise FriendlyError("Steam did not start", "CS2 needs the Steam client to start.",
                        "Start Steam yourself, log in, then try again.")


def match_folder(info: detect.DemoInfo) -> str:
    stamp = datetime.fromtimestamp(info.path.stat().st_mtime).strftime("%Y-%m-%d_%H%M")
    return f"{stamp}_{info.map_name}_{info.path.stem[-10:]}"


def playable_link(demo: Path) -> Path:
    """CS2 splits +playdemo on spaces, so expose every demo through a space-free symlink."""
    link = WORK_DIR / "demos" / demo.name.replace(" ", "_")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(demo.resolve())
    return link


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# --- main entry -------------------------------------------------------------------------------------

def analyze(cfg: Config, demo: Path) -> detect.DemoInfo:
    try:
        return detect.analyze(demo, cfg.accounts, round_types=cfg.round_highlights, special_kills=cfg.special_kills,
                              clutch_min_enemies=cfg.clutch_min_enemies, pre_seconds=cfg.pre_seconds,
                              post_seconds=cfg.post_seconds, max_gap_seconds=cfg.max_gap_seconds)
    except detect.NotYourMatch:
        raise
    except Exception as e:
        raise FriendlyError("Could not read the demo", f"{demo.name}: {e}",
                            "The file may be incomplete or corrupt. Download it again.") from e


def process(cfg: Config, demo: Path, rep: Reporter | None = None, mode: str | None = None) -> list[Path]:
    """Render every highlight of one demo. Returns the produced files."""
    rep = rep or Reporter()
    mode = mode or cfg.mode
    spec = MODES[mode]
    key = demo_key(demo)

    rep.stage("Finding highlights")
    rep.progress(None)
    try:
        info = analyze(cfg, demo)
    except detect.NotYourMatch as e:
        mark(key, skipped=str(e))
        raise FriendlyError("You are not in this demo",
                            "None of the Steam accounts used on this PC played in this match.",
                            "Pick one of your own matches, or add your SteamID under Settings → Accounts.") from e

    rep.log(f"{demo.name}: {info.map_name}, playing as {info.pov_name}, {len(info.highlights)} highlight(s)")
    for h in info.highlights:
        rep.log(f"  {h.name}  (score {h.score})")
    if not info.highlights:
        mark(key, highlights=0)
        rep.log("Nothing matched the highlight types picked in Settings → What counts as a highlight; "
                "nothing to record.")
        return []

    clips: list[Clip] = []
    takes_by_highlight: list[list[str]] = []
    for i, h in enumerate(info.highlights):
        names = []
        for k, (start, end) in enumerate(h.sequences):
            name = f"h{i:02d}s{k:02d}"
            clips.append(Clip(name, start, end, h.player_slot))
            names.append(name)
        takes_by_highlight.append(names)

    frames = sum((c.end_tick - c.start_tick) / info.tickrate * spec["render_fps"] for c in clips)
    raw_dir = WORK_DIR / "tmp/raw" / demo.stem
    shutil.rmtree(raw_dir, ignore_errors=True)
    raw_dir.parent.mkdir(parents=True, exist_ok=True)
    need = frames * AVG_JPG_BYTES * (cfg.width * cfg.height) / (1920 * 1080) * 1.2
    free = shutil.disk_usage(raw_dir.parent).free
    if free < need:
        raise FriendlyError("Not enough disk space",
                            f"Recording needs about {need / 1e9:.1f} GB in {WORK_DIR} but only "
                            f"{free / 1e9:.1f} GB is free.",
                            "Free some space, or re-run the installer pointing the workspace at a bigger disk.")

    if Cs2.is_running():
        rep.stage("Waiting for CS2 to close")
        rep.log("CS2 is open. Recording starts as soon as you close it.")
        while Cs2.is_running():
            time.sleep(5)
    ensure_steam(rep)

    rep.stage("Recording in CS2")
    rep.log("CS2 will open and close by itself. Leave it alone until it is done.")
    game = Cs2(cfg.cs2_path, PLUGIN_SO, raw_dir)
    started = time.monotonic()
    first_frame_at: list[float] = []

    def on_frames(done: int, total: int) -> None:
        if done and not first_frame_at:
            first_frame_at.append(time.monotonic())
        detail = f"{done}/{total} frames"
        if done and first_frame_at:
            rate = done / max(1e-6, time.monotonic() - first_frame_at[0])
            if rate > 0:
                detail += f", {rate:.0f} fps, ~{fmt_duration((total - done) / rate)} left"
        else:
            detail += ", CS2 is loading the demo"
        rep.progress(min(1.0, done / total) if total else None, detail)

    # Rendering runs well above 5 fps on any GPU that plays CS2; the margin covers seeks and loading.
    timeout = cfg.launch_timeout + frames / 5 + 30 * len(clips)
    if cfg.graphics == "high":
        rep.log("Using Very High graphics for the recording; your own settings come back afterwards")
    game.record(playable_link(demo), clips, spec["render_fps"], cfg.width, cfg.height, timeout,
                crosshair=cfg.crosshair, custom_crosshair=cfg.custom_crosshair,
                video_changes=videocfg.recording_changes(usercfg.active_account_cfg_dir(), cfg.graphics == "high"),
                log=rep.log, progress=on_frames)
    rep.log(f"Recorded {len(clips)} take(s) in {fmt_duration(time.monotonic() - started)}")

    rep.stage("Encoding clips")
    out_dir = cfg.clips_dir / match_folder(info)
    suffix = "" if mode == "smooth120" else f"_{mode}"
    outputs: list[Path] = []
    codecs = encode.codec_chain(cfg.video_codec)
    rep.log(f"Encoder: {encode.CODEC_NAMES[codecs[0]]}, {cfg.final_height}p")
    for n, (h, names) in enumerate(zip(info.highlights, takes_by_highlight)):
        rep.progress(n / len(info.highlights), f"clip {n + 1}/{len(info.highlights)}: {h.name}")
        try:
            takes = [(encode.find_take(raw_dir, name), name) for name in names]
        except FileNotFoundError as e:
            rep.log(f"  {h.name}: skipped, {e}")
            continue
        out = out_dir / f"{h.name}{suffix}.mp4"
        while True:
            try:
                encode.encode_clip(takes, out, spec["render_fps"], spec["blend"], codecs[0], cfg.quality,
                                   height=cfg.final_height if cfg.final_height != cfg.height else 0)
                break
            except subprocess.CalledProcessError as e:
                if len(codecs) == 1:
                    raise FriendlyError("Encoding failed", f"ffmpeg could not encode {h.name}: {e.stderr or e}",
                                        "Set Settings → Quality → Encoder to CPU and try again.") from e
                rep.log(f"  {encode.CODEC_NAMES[codecs[0]]} failed, trying {encode.CODEC_NAMES[codecs[1]]}")
                codecs.pop(0)
        outputs.append(out)
        rep.log(f"  wrote {out}")
    shutil.rmtree(raw_dir, ignore_errors=True)

    if not outputs:
        raise FriendlyError("No clips could be made", "CS2 ran but none of the highlights were recorded.",
                            f"Try again; if it repeats, check {game.plugin_log()} for errors.")

    if len(outputs) > 1:
        rep.stage("Building montage")
        montage = cfg.montages_dir / f"{match_folder(info)}{suffix}.mp4"
        encode.montage(outputs, montage)
        rep.log(f"  wrote {montage}")
        outputs.append(montage)

    rep.progress(1.0, "done")
    mark(key, highlights=len(info.highlights), mode=mode, files=[str(p) for p in outputs])
    return outputs
