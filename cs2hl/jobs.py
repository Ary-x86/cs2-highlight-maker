"""Persistent render queue for the GUI: jobs run one at a time, survive restarts, and can be reordered/removed.

A job is described by a JSON-able spec ({"kind": "latest"|"match"|"local", ...}) so the queue can be saved to
disk and rebuilt. Items that were running when the app stopped go back to "queued"; a restored queue starts
paused so CS2 doesn't pop up the moment the app opens.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from . import config, pipeline, steam
from .config import DOWNLOADS_DIR, WORK_DIR
from .errors import FriendlyError, explain

QUEUE_PATH = WORK_DIR / "queue.json"
LOG_KEEP = 400          # log lines kept per job on disk
HISTORY_KEEP = 50       # finished jobs kept in the list
ACTIVE = ("queued", "running")


class Cancelled(Exception):
    pass


@dataclass
class Job:
    id: int
    title: str
    spec: dict
    status: str = "queued"  # queued, running, done, failed, cancelled
    stage: str = ""
    progress: float | None = None
    detail: str = ""
    log: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    error: dict | None = None
    added: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0

    def to_dict(self, since: int = 0) -> dict:
        elapsed = (self.finished or time.time()) - self.started if self.started else 0
        return {"id": self.id, "title": self.title, "spec": self.spec, "mode": self.spec.get("mode"),
                "kind": self.spec.get("kind"),
                "status": self.status, "stage": self.stage, "progress": self.progress, "detail": self.detail,
                "log": self.log[since:], "log_total": len(self.log), "files": self.files, "error": self.error,
                "elapsed": pipeline.fmt_duration(elapsed) if elapsed else ""}

    def summary(self) -> dict:
        d = self.to_dict()
        d.pop("log")
        return d


class JobReporter(pipeline.Reporter):
    def __init__(self, runner: JobRunner, job: Job, cancel: threading.Event):
        self.runner = runner
        self.job = job
        self.cancel = cancel

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def note(self, msg: str) -> None:
        self.job.log.append(f"[{datetime.now():%H:%M:%S}] {msg}")
        self.runner.save()

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
    def __init__(self, path: Path = QUEUE_PATH):
        self.path = path
        self.jobs: list[Job] = []
        self.paused = False
        self.current: Job | None = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._next_id = 1
        self._load()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- persistence --------------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        names = {f.name for f in fields(Job)}
        for raw in data.get("jobs", []):
            job = Job(**{k: v for k, v in raw.items() if k in names})
            if job.status == "running":
                job.status, job.stage, job.progress = "queued", "Interrupted, will start again", None
            self.jobs.append(job)
        self._next_id = max((j.id for j in self.jobs), default=0) + 1
        self.paused = any(j.status == "queued" for j in self.jobs)

    def save(self) -> None:
        with self._lock:
            finished = [j for j in self.jobs if j.status not in ACTIVE]
            drop = {j.id for j in finished[:-HISTORY_KEEP]} if len(finished) > HISTORY_KEEP else set()
            self.jobs = [j for j in self.jobs if j.id not in drop]
            data = {"jobs": [{**asdict(j), "log": j.log[-LOG_KEEP:]} for j in self.jobs]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    # --- queue operations ----------------------------------------------------------------------------

    def get(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, spec: dict, title: str) -> Job:
        with self._lock:
            key = _spec_key(spec)
            for j in self.jobs:
                if j.status == "queued" and _spec_key(j.spec) == key and spec.get("mode") == j.spec.get("mode"):
                    return j  # already waiting
            job = Job(self._next_id, title, spec)
            self._next_id += 1
            self.jobs.append(job)
        self.save()
        self._wake.set()
        return job

    def remove(self, job_id: int) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            if job.status == "running":
                self._cancel.set()  # the worker marks it cancelled; it stays in history
                return
            self.jobs.remove(job)
        self.save()

    def move(self, job_id: int, delta: int) -> None:
        with self._lock:
            queued = [j for j in self.jobs if j.status == "queued"]
            job = self.get(job_id)
            if job not in queued:
                return
            i = queued.index(job)
            k = max(0, min(len(queued) - 1, i + delta))
            if i == k:
                return
            other = queued[k]
            a, b = self.jobs.index(job), self.jobs.index(other)
            self.jobs[a], self.jobs[b] = self.jobs[b], self.jobs[a]
        self.save()

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.status in ACTIVE]
        self.save()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._wake.set()

    @property
    def busy(self) -> bool:
        return self.current is not None or (not self.paused and any(j.status == "queued" for j in self.jobs))

    # --- worker ----------------------------------------------------------------------------------------

    def _next(self) -> Job | None:
        with self._lock:
            if self.paused:
                return None
            return next((j for j in self.jobs if j.status == "queued"), None)

    def _worker(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            self.current = job
            self._cancel.clear()
            rep = JobReporter(self, job, self._cancel)
            job.status, job.started, job.finished = "running", time.time(), 0.0
            job.error, job.files, job.progress = None, [], None
            try:
                files = build(job.spec)(rep) or []
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
                job.status, job.error, job.stage = "failed", friendly.to_dict(), "Failed"
                job.log.append(f"ERROR: {friendly.title}")
                if not isinstance(e, FriendlyError):
                    job.log.append(traceback.format_exc().rstrip())
            finally:
                job.finished = time.time()
                self.current = None
                self.save()


# --- job bodies -------------------------------------------------------------------------------------------

def _spec_key(spec: dict) -> str:
    if spec["kind"] == "match":
        return "match:" + spec["match"]["id"]
    if spec["kind"] == "local":
        return "local:" + spec["path"]
    return "latest"


def title_for(spec: dict) -> str:
    if spec["kind"] == "match":
        m = spec["match"]
        return f"{m['mode']} · {m['map']} · {m['time'][:10]}"
    if spec["kind"] == "local":
        return Path(spec["path"]).name
    return "Latest match"


def build(spec: dict):
    kind, mode = spec["kind"], spec.get("mode")
    if kind == "local":
        return render_local(Path(spec["path"]), mode)
    if kind == "match":
        names = {f.name for f in fields(steam.Match)}
        return render_match(steam.Match(**{k: v for k, v in spec["match"].items() if k in names}), mode)
    if kind == "latest":
        return render_match(None, mode)
    raise FriendlyError("Unknown job type", str(kind), "Remove it from the queue.")


def _cleanup(cfg: config.Config, dem: Path, rep: pipeline.Reporter) -> None:
    """Delete a demo this app downloaded or unpacked itself, unless the user keeps them."""
    if not cfg.keep_downloaded_demos:
        dem.unlink(missing_ok=True)
        rep.note("Deleted the downloaded demo (change this in Settings → Keep downloaded demos).")


def render_local(demo: Path, mode: str | None = None):
    def run(rep: pipeline.Reporter):
        if not demo.is_file():
            raise FriendlyError("Demo not found", str(demo), "It was moved or deleted; remove it from the queue.")
        cfg = config.load()
        if demo.name.endswith(".bz2"):
            rep.stage("Unpacking demo")
            dem = steam.unpack(demo, progress=rep.progress)
            try:
                return pipeline.process(cfg, dem, rep, mode)
            finally:
                _cleanup(cfg, dem, rep)
        return pipeline.process(cfg, demo, rep, mode)
    return run


def render_match(match: steam.Match | None, mode: str | None = None):
    """Get a match replay (newest one when match is None), render it, then drop the demo if we fetched it.

    A copy already on the PC (replays folder, ~/Downloads, .dem or .dem.bz2) is used instead of downloading.
    """
    def run(rep: pipeline.Reporter):
        cfg = config.load()
        session = steam.Session.load()
        if session is None:
            raise FriendlyError("Not logged in to Steam", "Match downloads come from your Steam account.",
                                "Open the Steam account page and log in.")
        target = match
        if target is None:
            rep.stage("Looking up your latest match")
            target = steam.latest_match(session)
            if target is None:
                raise FriendlyError("No downloadable matches", "Steam lists no recent match with a replay.",
                                    "Play a match first; replays expire after a few weeks.")
            rep.log(f"Latest: {target.mode} {target.map}, {target.time[:16].replace('T', ' ')} UTC, {target.score}")
        local = steam.find_local(target.id, steam.demo_search_dirs(cfg.replays_dir))
        ours = True  # downloaded/unpacked by this app, so it may be deleted afterwards
        if local and local.name.endswith(".bz2"):
            rep.stage(f"Unpacking {target.map} replay")
            rep.log(f"Found {local} on this PC, no download needed")
            dem = steam.unpack(local, progress=rep.progress)
        elif local:
            rep.log(f"Using {local} already on this PC, no download needed")
            dem, ours = local, local.parent == DOWNLOADS_DIR
        else:
            rep.stage(f"Downloading {target.map} replay")
            dem = steam.download(target, progress=rep.progress)
            rep.log(f"Downloaded {dem.name} ({dem.stat().st_size / 1e6:.0f} MB)")
        try:
            return pipeline.process(cfg, dem, rep, mode)
        finally:
            if ours:
                _cleanup(cfg, dem, rep)
    return run
