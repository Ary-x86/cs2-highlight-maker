"""cs2hl command line."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config, firstrun, pipeline
from .config import MODES, PLUGIN_SO
from .errors import FriendlyError, explain
from .game import Cs2, video_settings


def _demos(cfg: config.Config, paths: list[str], only_pending: bool) -> list[Path]:
    if paths:
        return [Path(p).expanduser().resolve() for p in paths]
    return pipeline.pending_demos(cfg) if only_pending else sorted(cfg.replays_dir.glob("*.dem"))


def cmd_gui(cfg, args) -> int:
    from .gui.server import serve
    serve(open_window=not args.no_window, port=args.port)
    return 0


def cmd_list(cfg, args) -> int:
    for demo in _demos(cfg, args.demos, only_pending=False):
        try:
            info = pipeline.analyze(cfg, demo)
        except Exception as e:  # not ours / unreadable: report and keep listing
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
        print("nothing to do")
        return 0
    failed = 0
    for demo in demos:
        try:
            pipeline.process(cfg, demo, mode=args.mode)
        except FriendlyError as e:
            failed += 1
            print(f"\n{demo.name}: {e}\n", file=sys.stderr)
    return 1 if failed else 0


def cmd_latest(cfg, args) -> int:
    from . import jobs
    jobs.render_match(None, args.mode)(pipeline.Reporter())
    return 0


def cmd_watch(cfg, args) -> int:
    print(f"watching {cfg.replays_dir} (mode {args.mode or cfg.mode})")
    seen_size: dict[Path, int] = {}
    while True:
        for demo in pipeline.pending_demos(cfg):
            size = demo.stat().st_size
            # Only touch a demo once it has stopped growing (download/extract finished).
            if seen_size.get(demo) != size or time.time() - demo.stat().st_mtime < 20:
                seen_size[demo] = size
                continue
            try:
                pipeline.process(cfg, demo, mode=args.mode)
            except FriendlyError as e:
                print(f"\n{demo.name}: {e}\n", file=sys.stderr)
                pipeline.mark(pipeline.demo_key(demo), error=e.title)
        time.sleep(args.interval)


def cmd_clean(cfg, args) -> int:
    """Undo plugin install if a run was interrupted hard."""
    if Cs2.is_running():
        print("CS2 is running; close it first")
        return 1
    Cs2(cfg.cs2_path, PLUGIN_SO, config.WORK_DIR / "tmp/raw").uninstall()
    video_settings().restore()
    print("plugin removed, gameinfo.gi and your video settings restored")
    return 0


def cmd_config(cfg, args) -> int:
    print(f"config file: {config.CONFIG_PATH} ({'exists' if config.CONFIG_PATH.exists() else 'defaults'})")
    for key, value in cfg.to_dict().items():
        print(f"  {key} = {value!r}")
    print(f"  -> CS2: {cfg.cs2_path}")
    print(f"  -> clips: {cfg.clips_dir}  montages: {cfg.montages_dir}")
    print(f"plugin: {PLUGIN_SO} ({'ok' if PLUGIN_SO.exists() else 'MISSING, run ./install.sh'})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cs2hl", description="Automatic CS2 highlight clips from demos")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="open the graphical interface (default)")
    p.add_argument("--no-window", action="store_true", help="only start the server, print its URL")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("list", help="show detected highlights without rendering")
    p.add_argument("demos", nargs="*", help="demo files (default: all in the replays folder)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="render highlights for new local demos (or the given ones)")
    p.add_argument("demos", nargs="*")
    p.add_argument("--mode", choices=list(MODES), help="smooth120 (default) or blur60 motion blur")
    p.add_argument("--force", action="store_true", help="also redo demos that were already processed")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("latest", help="download your latest match from Steam and render it")
    p.add_argument("--mode", choices=list(MODES))
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("watch", help="keep running and process demos as they appear in the replays folder")
    p.add_argument("--mode", choices=list(MODES))
    p.add_argument("--interval", type=int, default=30, help="seconds between folder scans")
    p.set_defaults(func=cmd_watch)

    sub.add_parser("clean", help="remove the CS2 plugin after an interrupted run").set_defaults(func=cmd_clean)
    sub.add_parser("config", help="print effective settings").set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["gui", *(argv or [])])
    try:
        firstrun.provision_plugin()
        cfg = config.load()
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - show every failure the friendly way
        print(f"\n{explain(e)}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
