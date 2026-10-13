"""Demo -> highlights -> recorded takes -> encoded clips + montage."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import detect, encode
from .config import CLIPS_DIR, MONTAGES_DIR, PLUGIN_SO, STATE_PATH, WORK_DIR, Config, MODES
from .game import Clip, Cs2

AVG_JPG_BYTES = 250_000  # 1080p frame at jpeg_quality 100 is ~160KB; leave headroom


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- state ------------------------------------------------------------------------------------------

def demo_key(demo: Path) -> str:
    return f"{demo.name}:{demo.stat().st_size}"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def pending_demos(cfg: Config) -> list[Path]:
    state = load_state()
    demos = sorted(cfg.replays_dir.glob("*.dem"), key=lambda p: p.stat().st_mtime)
    return [d for d in demos if demo_key(d) not in state]


# --- helpers ----------------------------------------------------------------------------------------

def ensure_steam() -> None:
    if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0:
        return
    log("starting Steam")
    subprocess.Popen(["steam", "-silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(45)


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


# --- main entry -------------------------------------------------------------------------------------

def analyze(cfg: Config, demo: Path) -> detect.DemoInfo:
    return detect.analyze(demo, cfg.steamids, min_kills=cfg.min_kills,
                          clutch_min_enemies=cfg.clutch_min_enemies, pre_seconds=cfg.pre_seconds,
                          post_seconds=cfg.post_seconds, max_gap_seconds=cfg.max_gap_seconds)


def process(cfg: Config, demo: Path, mode: str | None = None) -> list[Path]:
    """Render every highlight of one demo. Returns the produced files."""
    mode = mode or cfg.mode
    spec = MODES[mode]
    state = load_state()
    key = demo_key(demo)

    try:
        info = analyze(cfg, demo)
    except ValueError as e:  # not one of our matches
        log(f"skip {demo.name}: {e}")
        state[key] = {"skipped": str(e)}
        save_state(state)
        return []

    log(f"{demo.name}: {info.map_name}, {info.pov_name}, {len(info.highlights)} highlight(s)")
    if not info.highlights:
        state[key] = {"highlights": 0}
        save_state(state)
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
        log(f"  {h.name}  score {h.score}")

    frames = sum((c.end_tick - c.start_tick) / info.tickrate * spec["render_fps"] for c in clips)
    raw_dir = WORK_DIR / "tmp/raw" / demo.stem
    need = frames * AVG_JPG_BYTES * 1.2
    shutil.rmtree(raw_dir, ignore_errors=True)
    raw_dir.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(raw_dir.parent).free
    if free < need:
        raise RuntimeError(f"need ~{need / 1e9:.1f} GB free for frames, only {free / 1e9:.1f} GB")

    while Cs2.is_running():
        log("CS2 is running; waiting for it to close")
        time.sleep(30)
    ensure_steam()

    game = Cs2(cfg.cs2_dir, PLUGIN_SO, raw_dir)
    started = time.monotonic()
    # Rendering runs well above 20 fps on any GPU that plays CS2; the margin covers seeks and loading.
    timeout = cfg.launch_timeout + frames / 20 + 30 * len(clips)
    game.record(playable_link(demo), clips, spec["render_fps"], cfg.width, cfg.height, timeout, log=log)
    log(f"recorded {len(clips)} take(s) in {time.monotonic() - started:.0f}s")

    out_dir = CLIPS_DIR / match_folder(info)
    suffix = "" if mode == "smooth120" else f"_{mode}"
    outputs: list[Path] = []
    for h, names in zip(info.highlights, takes_by_highlight):
        try:
            takes = [(encode.find_take(raw_dir, n), n) for n in names]
        except FileNotFoundError as e:
            log(f"  {h.name}: {e}")
            continue
        out = out_dir / f"{h.name}{suffix}.mp4"
        encode.encode_clip(takes, out, spec["render_fps"], spec["blend"], cfg.video_codec, cfg.quality)
        outputs.append(out)
        log(f"  wrote {out}")
    shutil.rmtree(raw_dir, ignore_errors=True)

    if not outputs:
        raise RuntimeError("CS2 recorded nothing; check the plugin log: " + str(game.plugin_log()))

    if len(outputs) > 1:
        montage = MONTAGES_DIR / f"{match_folder(info)}{suffix}.mp4"
        encode.montage(outputs, montage)
        log(f"  wrote {montage}")
        outputs.append(montage)

    state = load_state()
    state[key] = {"highlights": len(info.highlights), "mode": mode, "files": [str(p) for p in outputs],
                  "done": datetime.now().isoformat(timespec="seconds")}
    save_state(state)
    return outputs
