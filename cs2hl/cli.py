"""cs2hl command line."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config, pipeline
from .config import MODES, PLUGIN_SO
from .game import Cs2
from .pipeline import log


def _demos(cfg: config.Config, paths: list[str], only_pending: bool) -> list[Path]:
    if paths:
        return [Path(p).expanduser().resolve() for p in paths]
    return pipeline.pending_demos(cfg) if only_pending else sorted(cfg.replays_dir.glob("*.dem"))


def cmd_list(cfg, args) -> int:
    for demo in _demos(cfg, args.demos, only_pending=False):
        try:
            info = pipeline.analyze(cfg, demo)
        except ValueError as e:
            print(f"{demo.name}: skipped ({e})")
            continue
        print(f"{demo.name}: {info.map_name}, POV {info.pov_name}")
        for h in info.highlights:
            secs = sum(e - s for s, e in h.sequences) / info.tickrate
            kills = ", ".join(f"{k.weapon}{'(hs)' if k.headshot else ''}{'+' + '+'.join(k.flags) if k.flags else ''}"
                              for k in h.kills)
            print(f"  {h.name:<28} score {h.score:>4}  {secs:4.1f}s  {kills}")
    return 0


def cmd_run(cfg, args) -> int:
    demos = _demos(cfg, args.demos, only_pending=not args.force)
    if not demos:
        log("nothing to do")
        return 0
    failed = 0
    for demo in demos:
        try:
            pipeline.process(cfg, demo, args.mode)
        except Exception as e:  # keep going with the other demos
            failed += 1
            log(f"FAILED {demo.name}: {e}")
    return 1 if failed else 0


def cmd_watch(cfg, args) -> int:
    log(f"watching {cfg.replays_dir} (mode {args.mode or cfg.mode})")
    seen_size: dict[Path, int] = {}
    while True:
        for demo in pipeline.pending_demos(cfg):
            size = demo.stat().st_size
            # Only touch a demo once it has stopped growing (download/extract finished).
            if seen_size.get(demo) != size or time.time() - demo.stat().st_mtime < 20:
                seen_size[demo] = size
                continue
            try:
                pipeline.process(cfg, demo, args.mode)
            except Exception as e:
                log(f"FAILED {demo.name}: {e}")
                state = pipeline.load_state()
                state[pipeline.demo_key(demo)] = {"error": str(e)}
                pipeline.save_state(state)
        time.sleep(args.interval)


def cmd_clean(cfg, args) -> int:
    """Undo plugin install if a run was interrupted hard."""
    if Cs2.is_running():
        print("CS2 is running; close it first")
        return 1
    Cs2(cfg.cs2_dir, PLUGIN_SO, config.WORK_DIR / "tmp/raw").uninstall()
    print("plugin removed, gameinfo.gi restored")
    return 0


def cmd_config(cfg, args) -> int:
    print(f"config file: {config.CONFIG_PATH} ({'exists' if config.CONFIG_PATH.exists() else 'defaults'})")
    for key, value in vars(cfg).items():
        print(f"  {key} = {value!r}")
    print(f"  replays_dir -> {cfg.replays_dir}")
    print(f"plugin: {PLUGIN_SO} ({'ok' if PLUGIN_SO.exists() else 'MISSING, run plugin/build.sh'})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cs2hl", description="Automatic CS2 highlight clips from demos")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="show detected highlights without rendering")
    p.add_argument("demos", nargs="*", help="demo files (default: all in the replays folder)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="render highlights for new demos (or the given ones)")
    p.add_argument("demos", nargs="*")
    p.add_argument("--mode", choices=list(MODES), help="smooth120 (default) or blur60 motion blur")
    p.add_argument("--force", action="store_true", help="also redo demos that were already processed")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", help="keep running and process demos as they appear")
    p.add_argument("--mode", choices=list(MODES))
    p.add_argument("--interval", type=int, default=30, help="seconds between folder scans")
    p.set_defaults(func=cmd_watch)

    sub.add_parser("clean", help="remove the CS2 plugin after an interrupted run").set_defaults(func=cmd_clean)
    sub.add_parser("config", help="print effective settings").set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    cfg = config.load()
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
