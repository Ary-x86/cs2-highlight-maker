"""Background jobs for the GUI: one at a time, with a log, stage, progress and a friendly error."""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, pipeline, steam
from .errors import FriendlyError, explain


class Cancelled(Exception):
    pass


@dataclass
class Job:
    id: int
    title: str
    status: str = "queued"  # queued, running, done, failed, cancelled
    stage: str = ""
    progress: float | None = None
    detail: str = ""
    log: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    error: dict | None = None
    started: float = 0.0
    finished: float = 0.0

    def to_dict(self, since: int = 0) -> dict:
        elapsed = (self.finished or time.time()) - self.started if self.started else 0
        return {"id": self.id, "title": self.title, "status": self.status, "stage": self.stage,
                "progress": self.progress, "detail": self.detail, "log": self.log[since:],
                "log_total": len(self.log), "files": self.files, "error": self.error,
                "elapsed": pipeline.fmt_duration(elapsed) if elapsed else ""}


class JobReporter(pipeline.Reporter):
    def __init__(self, job: Job, cancel: threading.Event):
        self.job = job
        self.cancel = cancel

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def note(self, msg: str) -> None:
        self.job.log.append(f"[{datetime.now():%H:%M:%S}] {msg}")

    def log(self, msg: str) -> None:
        self.note(msg)
        self._check()

    def stage(self, name: str) -> None:
        self.job.stage = name
        self.job.progress = None
        self.job.detail = ""
        self.log(f"— {name}")

    def progress(self, fraction: float | None, detail: str = "") -> None:
        self.job.progress = fraction
        self.job.detail = detail
        self._check()


class JobRunner:
    def __init__(self):
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._queue: list[tuple[Job, callable]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self.current: Job | None = None
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, title: str, fn) -> Job:
        job = Job(next(self._ids), title)
        with self._lock:
            self.jobs[job.id] = job
            self._queue.append((job, fn))
        self._wake.set()
        return job

    def cancel(self, job_id: int) -> None:
        with self._lock:
            for i, (job, _) in enumerate(self._queue):
                if job.id == job_id:
                    job.status = "cancelled"
                    del self._queue[i]
                    return
        if self.current and self.current.id == job_id:
            self._cancel.set()

    def _worker(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if not self._queue:
                    self._wake.clear()
                    continue
                job, fn = self._queue.pop(0)
            self.current = job
            self._cancel.clear()
            rep = JobReporter(job, self._cancel)
            job.status, job.started = "running", time.time()
            try:
                files = fn(rep) or []
                job.files = [str(f) for f in files]
                job.status = "done"
                job.stage = "Done" if files else "Done, no highlights found"
                job.progress = 1.0
            except Cancelled:
                from .game import Cs2
                Cs2.kill()
                job.status, job.stage = "cancelled", "Cancelled"
                job.log.append("Cancelled.")
            except BaseException as e:  # noqa: BLE001 - every failure must reach the GUI
                friendly = explain(e)
                job.status, job.error = "failed", friendly.to_dict()
                job.stage = "Failed"
                job.log.append(f"ERROR: {friendly.title}")
                if not isinstance(e, FriendlyError):
                    job.log.append(traceback.format_exc().rstrip())
            finally:
                job.finished = time.time()
                self.current = None


# --- job bodies -------------------------------------------------------------------------------------------

def render_local(demo: Path, mode: str | None = None):
    def run(rep: pipeline.Reporter):
        cfg = config.load()
        return pipeline.process(cfg, demo, rep, mode)
    return run


def render_match(match: steam.Match | None, mode: str | None = None):
    """Download a match replay (or the newest one when match is None), render it, then drop the demo."""
    def run(rep: pipeline.Reporter):
        cfg = config.load()
        session = steam.Session.load()
        if session is None:
            raise FriendlyError("Not logged in to Steam", "Match downloads come from your Steam account.",
                                "Open the Steam tab and log in.")
        target = match
        if target is None:
            rep.stage("Looking up your latest match")
            matches = [m for m in steam.match_history(session) if m.url]
            if not matches:
                raise FriendlyError("No downloadable matches", "Steam lists no recent match with a replay.",
                                    "Play a Premier/Competitive/Wingman match first; replays expire after a few weeks.")
            target = matches[0]
            rep.log(f"Latest: {target.mode} {target.map}, {target.time[:16].replace('T', ' ')} UTC, {target.score}")
        rep.stage(f"Downloading {target.map} replay")
        dem = steam.download(target, progress=rep.progress)
        rep.log(f"Downloaded {dem.name} ({dem.stat().st_size / 1e6:.0f} MB)")
        try:
            return pipeline.process(cfg, dem, rep, mode)
        finally:
            if not cfg.keep_downloaded_demos:
                dem.unlink(missing_ok=True)
                rep.note("Deleted the downloaded demo (change this in Settings → Keep downloaded demos).")
    return run
