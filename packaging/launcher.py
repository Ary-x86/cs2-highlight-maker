"""Entry point of the packaged app (PyInstaller). Without arguments it opens the GUI, like `cs2hl`."""

import sys

from cs2hl import system
from cs2hl.config import WORK_DIR

if system.FROZEN and (sys.stdout is None or sys.stderr is None):
    # The Windows app has no console window: keep its output in a log file instead.
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(WORK_DIR / "app.log", "a", buffering=1, encoding="utf-8")

from cs2hl.cli import main  # noqa: E402

sys.exit(main())
