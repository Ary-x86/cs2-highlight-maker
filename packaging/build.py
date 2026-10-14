"""Build the packaged app (a folder with CS2HighlightMaker[.exe] inside) with PyInstaller.

    python packaging/build.py --plugin path/to/server.dll   (Windows)
    python packaging/build.py --plugin path/to/libserver.so (Linux)

The recording plugin is bundled so the app can install it without a compiler; see plugin/build.sh and
plugin/build.ps1 for building it.
"""

import argparse
import os
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser()
parser.add_argument("--plugin", type=Path, help="prebuilt recording plugin to bundle")
parser.add_argument("--name", default="CS2HighlightMaker")
args = parser.parse_args()

sep = os.pathsep
opts = [
    str(ROOT / "packaging/launcher.py"),
    "--name", args.name,
    "--onedir",  # a folder starts much faster than a single self-extracting file (the parser libs are big)
    "--noconfirm",
    "--clean",
    "--paths", str(ROOT),
    "--add-data", f"{ROOT / 'cs2hl/gui/static'}{sep}cs2hl/gui/static",
    "--collect-submodules", "cs2hl",
    # demoparser2 hands its results over through Arrow, imported from native code PyInstaller can't see.
    "--hidden-import", "pyarrow",
    "--collect-submodules", "pyarrow",
    "--hidden-import", "pandas",
    "--hidden-import", "polars",
    "--collect-submodules", "polars",
    "--collect-binaries", "_polars_runtime_32",
    "--collect-submodules", "_polars_runtime_32",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
]
if sys.platform == "win32":
    opts += ["--windowed", "--icon", str(ROOT / "assets/icon.ico")]
if args.plugin:
    opts += ["--add-binary", f"{args.plugin.resolve()}{sep}plugin"]
PyInstaller.__main__.run(opts)
