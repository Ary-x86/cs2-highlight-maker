"""Local web GUI: a small JSON API on 127.0.0.1 plus static files, opened in an app-style browser window.

Every API call must carry the random per-launch token, so other websites open in the browser cannot drive it.
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import segno

from .. import config, encode, firstrun, jobs, pipeline, steam, system
from ..config import MODES, PLUGIN_SO, STEAM_ROOT, WORK_DIR
from ..errors import FriendlyError, explain
from ..game import CROSSHAIRS, Cs2, video_settings

STATIC = Path(__file__).parent / "static"
# GPU encoders offered in Settings on this OS (VAAPI is Linux-only, AMF and Quick Sync are offered on Windows).
PLATFORM_CODECS = ("h264_nvenc", "h264_amf", "h264_qsv") if system.IS_WINDOWS else ("h264_nvenc", "h264_vaapi")
IDLE_EXIT_SECONDS = 90  # quit when no window has polled for this long and nothing is running
MATCH_ID = re.compile(r"\d{21}_\d{10}")  # Valve replay names: <match id>_<reservation>
APP_BROWSERS = ("brave", "brave-browser", "chromium", "chromium-browser", "google-chrome-stable",
                "google-chrome", "microsoft-edge", "vivaldi")
# Chromium-based browsers can open the app as its own window (--app). Windows and macOS don't put them on PATH.
if system.IS_WINDOWS:
    APP_BROWSER_PATHS = [Path(os.environ.get(root, "-")) / sub for root in ("ProgramFiles(x86)", "ProgramFiles",
                                                                             "LOCALAPPDATA")
                         for sub in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe",
                                     "BraveSoftware/Brave-Browser/Application/brave.exe")]
elif system.IS_MAC:
    APP_BROWSER_PATHS = [Path(f"/Applications/{app}.app/Contents/MacOS/{app}")
                         for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")]
else:
    APP_BROWSER_PATHS = []


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(18)
        self.runner = jobs.JobRunner()
        self.login: steam.QrLogin | steam.PasswordLogin | None = None
        self.login_error: dict | None = None
        self.histories: dict[str, steam.TabHistory] = {}
        self.last_seen = time.time()
        self.config_lock = threading.Lock()  # requests run in parallel; config edits must not overwrite each other
        self.ffmpeg_download = firstrun.FfmpegDownload()

    # --- helpers ----------------------------------------------------------------------------------

    @staticmethod
    def account_names() -> dict[str, str]:
        vdf = STEAM_ROOT / "config/loginusers.vdf"
        if not vdf.exists():
            return {}
        text = vdf.read_text(errors="ignore")
        return {sid: (re.search(r'"PersonaName"\s+"([^"]*)"', body) or [None, sid])[1]
                for sid, body in re.findall(r'"(7656\d{13})"\s*\{([^}]*)\}', text)}

    def state(self) -> dict:
        cfg = config.load()
        try:
            cs2 = str(cfg.cs2_path)
            cs2_error = None
        except FriendlyError as e:
            cs2, cs2_error = None, e.to_dict()
        session = steam.Session.load()
        current = self.runner.current
        return {
            "config": cfg.to_dict(),
            "cs2_dir": cs2,
            "cs2_error": cs2_error,
            "installs": config.find_cs2_installs(),
            "accounts": [{"steamid": s, "name": n} for s, n in self.account_names().items()],
            "session": {"steamid": session.steamid, "account_name": session.account_name} if session else None,
            "modes": MODES,
            "crosshairs": CROSSHAIRS,
            "clips_dir": str(cfg.clips_dir),
            "montages_dir": str(cfg.montages_dir),
            "output_path": str(cfg.output_path),
            "replays_dir": str(cfg.replays_dir) if cs2 else None,
            "codecs": {c: encode.CODEC_NAMES.get(c, "Automatic") for c in config.CODECS
                       if c in ("auto", "libx264") or c in PLATFORM_CODECS},
            "setup": {**firstrun.checks(), "ffmpeg_download": self.ffmpeg_download.status()},
            "work_dir": str(WORK_DIR.resolve()),
            "plugin_ok": PLUGIN_SO.exists(),
            "steam_running": pipeline.steam_running(),
            "cs2_running": Cs2.is_running(),
            "current_job": current.id if current else None,
            "queue_waiting": sum(1 for j in self.runner.jobs if j.status == "queued"),
            "queue_paused": self.runner.paused,
            "match_tabs": steam.MATCH_TABS,
            "match_tab_urls": {tab: steam.gcpd_url(session.steamid if session else None, tab)
                               for tab in steam.MATCH_TABS},
        }

    def save_config(self, changes: dict) -> dict:
        with self.config_lock:
            self._save_config(changes)
        return self.state()

    def _save_config(self, changes: dict) -> None:
        cfg = config.load()
        known = cfg.to_dict()
        for key, value in changes.items():
            if key not in known:
                raise FriendlyError("Unknown setting", key, "Reload the window.")
            setattr(cfg, key, value)
        if cfg.cs2_dir:
            cfg.cs2_path  # raises a friendly error when the folder is wrong
        if cfg.output_dir:
            out = Path(cfg.output_dir).expanduser()
            try:
                out.mkdir(parents=True, exist_ok=True)
                probe = out / ".cs2hl-write-test"
                probe.write_text("")
                probe.unlink()
            except OSError as e:
                raise FriendlyError("Cannot use that output folder", str(e),
                                    "Pick a folder you can write to, on a disk with free space.")
        config.save(cfg)

    def matches(self, tab: str, refresh: bool, more: bool) -> dict:
        session = steam.Session.load()
        if session is None:
            raise FriendlyError("Not logged in to Steam", "The match list comes from your Steam account.",
                                "Open the Steam account page and log in.")
        history = self.histories.get(tab)
        if refresh or history is None or history.session.steamid != session.steamid:
            history = self.histories[tab] = steam.TabHistory(session, tab)
        if not history.started or more:
            history.load(min_new=5, max_requests=6)
        made = rendered_demos()
        queued = {jobs._spec_key(j.spec) for j in self.runner.jobs if j.status in jobs.ACTIVE}
        search = steam.demo_search_dirs(self._replays_dir())
        out = []
        for m in history.matches:
            local = steam.find_local(m.id, search)
            files = files_for(m.id, made)
            out.append({**m.__dict__, "rendered": bool(files), "files": files, "queued": f"match:{m.id}" in queued,
                        "local": str(local) if local else None})
        return {"matches": out, "reached": history.reached, "exhausted": history.exhausted}

    @staticmethod
    def _replays_dir() -> Path | None:
        try:
            return config.load().replays_dir
        except FriendlyError:
            return None

    def queue(self) -> dict:
        current = self.runner.current
        return {"paused": self.runner.paused, "current": current.id if current else None,
                "items": [j.summary() for j in self.runner.jobs]}

    def add_to_queue(self, items: list[dict], mode: str | None) -> dict:
        if mode is not None and mode not in MODES:
            raise FriendlyError("Unknown video mode", mode, "Pick a mode in Settings.")
        ids = []
        for item in items:
            kind = item.get("kind")
            if kind == "match":
                known = {f.name for f in dataclasses.fields(steam.Match)}
                spec = {"kind": "match", "match": {k: v for k, v in item["match"].items() if k in known}}
                if not spec["match"].get("url") and not steam.find_local(
                        spec["match"].get("id", ""), steam.demo_search_dirs(self._replays_dir())):
                    raise FriendlyError("Replay not available", f"{spec['match'].get('map')} has no replay to download.",
                                        "Valve only keeps replays for a few weeks; pick a newer match.")
            elif kind == "local":
                if not Path(item["path"]).is_file():
                    raise FriendlyError("Demo not found", item["path"], "Refresh the list.")
                spec = {"kind": "local", "path": item["path"]}
            elif kind == "latest":
                spec = {"kind": "latest"}
            else:
                raise FriendlyError("Unknown job", str(kind), "Reload the window.")
            spec["mode"] = mode or config.load().mode
            ids.append(self.runner.add(spec, jobs.title_for(spec)).id)
        return {"ids": ids, "paused": self.runner.paused}

    def local_demos(self) -> list[dict]:
        cfg = config.load()
        state = pipeline.load_state()
        made = rendered_demos()
        queued = {jobs._spec_key(j.spec) for j in self.runner.jobs if j.status in jobs.ACTIVE}
        demos = list(cfg.replays_dir.glob("*.dem"))
        def match_key(p: Path) -> str:
            m = MATCH_ID.search(p.name)
            return m.group() if m else p.name.removesuffix(".bz2")
        have = {match_key(d) for d in demos}
        # Compressed replays (e.g. from the Steam website), unless that match is already unpacked next to it.
        demos += [z for z in cfg.replays_dir.glob("*.dem.bz2") if match_key(z) not in have]
        out = []
        for dem in sorted(demos, key=lambda p: p.stat().st_mtime, reverse=True):
            stem = dem.name.removesuffix(".bz2")
            info = state.get(pipeline.demo_key(dem), {})
            files = files_for(stem, made)
            out.append({"path": str(dem), "name": dem.name, "size_mb": round(dem.stat().st_size / 1e6),
                        "modified": datetime.fromtimestamp(dem.stat().st_mtime).isoformat(timespec="minutes"),
                        "compressed": dem.name.endswith(".bz2"), "rendered": bool(files), "files": files,
                        "queued": f"local:{dem}" in queued, "skipped": info.get("skipped")})
        return out

    def outputs(self) -> list[dict]:
        cfg = config.load()
        items = []
        for folder in (cfg.montages_dir, cfg.clips_dir):
            if folder.exists():
                for f in folder.rglob("*.mp4"):
                    items.append({"path": str(f.resolve()), "name": f.name, "group": f.parent.name,
                                  "kind": "montage" if folder == cfg.montages_dir else "clip",
                                  "size_mb": round(f.stat().st_size / 1e6), "mtime": f.stat().st_mtime})
        return sorted(items, key=lambda i: i["mtime"], reverse=True)[:200]

    @staticmethod
    def browse(path: str) -> dict:
        p = Path(path).expanduser() if path else Path.home()
        if not p.is_dir():
            p = p.parent if p.parent.is_dir() else Path.home()
        dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
        if system.IS_WINDOWS:
            roots = [str(Path.home())] + [f"{d}:\\" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{d}:/").exists()]
        elif system.IS_MAC:
            roots = [str(Path.home())] + [str(m) for m in Path("/Volumes").glob("*") if m.is_dir()]
        else:
            roots = [str(Path.home())] + [str(m) for m in Path("/run/media", os.environ.get("USER", "")).glob("*")
                                          if m.is_dir()] + ["/mnt", "/media"]
        return {"path": str(p), "parent": str(p.parent), "dirs": dirs[:500],
                "is_cs2": (p / system.CS2_LAUNCHER).exists(), "roots": [r for r in roots if Path(r).is_dir()]}

    @staticmethod
    def open_url(url: str) -> None:
        if not url.startswith(steam.COMMUNITY + "/"):
            raise FriendlyError("Link not allowed", url, "Only steamcommunity.com pages can be opened from here.")
        system.open_target(url)

    @staticmethod
    def open_path(path: str, reveal: bool) -> None:
        p = Path(path)
        target = p.parent if reveal and p.is_file() else p
        if not target.exists():
            raise FriendlyError("File not found", str(p), "It may have been moved or deleted.")
        system.open_target(str(target))

    # --- login ------------------------------------------------------------------------------------

    def login_status(self) -> dict:
        session = steam.Session.load()
        if self.login is None:
            return {"state": "done" if session else "idle", "error": self.login_error,
                    "account_name": session.account_name if session else None}
        try:
            new = self.login.poll()
        except FriendlyError as e:
            self.login, self.login_error = None, e.to_dict()
            return {"state": "error", "error": self.login_error}
        except Exception as e:  # noqa: BLE001
            self.login, self.login_error = None, explain(e).to_dict()
            return {"state": "error", "error": self.login_error}
        if new:
            self.login, self.histories = None, {}
            return {"state": "done", "account_name": new.account_name}
        result = {"state": "waiting"}
        if isinstance(self.login, steam.QrLogin):
            if time.time() - self.login.started > 170:  # QR challenges expire; start a new one
                self.login = steam.QrLogin()
            result["qr"] = segno.make(self.login.challenge_url, error="m").svg_inline(
                scale=6, dark="#0b0f14", light="#ffffff", border=2)
        else:
            result["needs_code"] = self.login.needs_code
        return result


def rendered_demos() -> list[tuple[str, float, list[str]]]:
    """(demo file name, when, existing output files) for every demo that produced videos."""
    out = []
    for key, info in pipeline.load_state().items():
        # resolved, so they compare equal to outputs() even when saved through the workspace symlink
        files = [str(Path(f).resolve()) for f in info.get("files") or [] if Path(f).exists()]
        if files:
            out.append((key.rsplit(":", 1)[0], datetime.fromisoformat(info["at"]).timestamp() if info.get("at") else 0,
                        files))
    return out


def files_for(fragment: str, made: list[tuple[str, float, list[str]]]) -> list[str]:
    """Videos of the newest render whose demo name contains fragment (a match id or demo name)."""
    hits = [(at, files) for name, at, files in made if fragment in name]
    return max(hits, default=(0, []))[1]


def _find_app_browser() -> str | None:
    for name in APP_BROWSERS:
        if path := shutil.which(name):
            return path
    return next((str(p) for p in APP_BROWSER_PATHS if p.exists()), None)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cs2hl"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # --- plumbing ------------------------------------------------------------------------------

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def _error(self, exc: BaseException) -> None:
            friendly = explain(exc)
            self._json({"error": friendly.to_dict()}, 400 if isinstance(exc, FriendlyError) else 500)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}") if length else {}

        def _authorized(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost") and \
                secrets.compare_digest(self.headers.get("X-Token", ""), app.token)

        def _static(self, rel: str) -> None:
            path = (STATIC / (rel or "index.html")).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

        # --- routing -----------------------------------------------------------------------------------

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path.lstrip("/"))
            if not self._authorized():
                return self._json({"error": {"title": "Unauthorized", "why": "", "fix": ""}}, 403)
            app.last_seen = time.time()
            q = dict(urllib.parse.parse_qsl(url.query))
            try:
                if url.path == "/api/state":
                    return self._json(app.state())
                if url.path == "/api/matches":
                    return self._json(app.matches(q.get("tab", next(iter(steam.MATCH_TABS))),
                                                  q.get("refresh") == "1", q.get("more") == "1"))
                if url.path == "/api/queue":
                    return self._json(app.queue())
                if url.path == "/api/local":
                    return self._json(app.local_demos())
                if url.path == "/api/outputs":
                    return self._json(app.outputs())
                if url.path == "/api/browse":
                    return self._json(app.browse(q.get("path", "")))
                if url.path == "/api/login/status":
                    return self._json(app.login_status())
                if m := re.fullmatch(r"/api/jobs/(\d+)", url.path):
                    job = app.runner.get(int(m.group(1)))
                    if job is None:
                        return self._json({"error": {"title": "No such job", "why": "", "fix": ""}}, 404)
                    return self._json(job.to_dict(int(q.get("since", 0))))
                self._json({"error": {"title": "Not found", "why": url.path, "fix": ""}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if not self._authorized():
                return self._json({"error": {"title": "Unauthorized", "why": "", "fix": ""}}, 403)
            app.last_seen = time.time()
            try:
                body = self._body()
                if url.path == "/api/config":
                    return self._json(app.save_config(body))
                if url.path == "/api/queue/add":
                    return self._json(app.add_to_queue(body.get("items", []), body.get("mode")))
                if m := re.fullmatch(r"/api/queue/(\d+)/remove", url.path):
                    app.runner.remove(int(m.group(1)))
                    return self._json(app.queue())
                if m := re.fullmatch(r"/api/queue/(\d+)/move", url.path):
                    app.runner.move(int(m.group(1)), int(body.get("delta", 0)))
                    return self._json(app.queue())
                if url.path == "/api/queue/clear":
                    app.runner.clear_finished()
                    return self._json(app.queue())
                if url.path == "/api/queue/pause":
                    app.runner.set_paused(bool(body.get("paused")))
                    return self._json(app.queue())
                if url.path == "/api/login/qr":
                    app.login, app.login_error = steam.QrLogin(), None
                    return self._json(app.login_status())
                if url.path == "/api/login/password":
                    app.login, app.login_error = steam.PasswordLogin(body["account"], body["password"]), None
                    return self._json(app.login_status())
                if url.path == "/api/login/code":
                    if not isinstance(app.login, steam.PasswordLogin):
                        raise FriendlyError("No login in progress", "", "Start the login again.")
                    app.login.submit_code(body["code"])
                    return self._json(app.login_status())
                if url.path == "/api/login/cancel":
                    app.login = None
                    return self._json({"ok": True})
                if url.path == "/api/logout":
                    steam.Session.logout()
                    app.histories = {}
                    return self._json({"ok": True})
                if url.path == "/api/open":
                    app.open_path(body["path"], bool(body.get("reveal")))
                    return self._json({"ok": True})
                if url.path == "/api/open_url":
                    app.open_url(body["url"])
                    return self._json({"ok": True})
                if url.path == "/api/setup/done":
                    with app.config_lock:
                        if not config.CONFIG_PATH.exists():
                            config.save(config.load())
                    return self._json(app.state())
                if url.path == "/api/setup/ffmpeg":
                    app.ffmpeg_download.start()
                    return self._json(app.state())
                if url.path == "/api/clean":
                    if Cs2.is_running():
                        raise FriendlyError("CS2 is running", "The plugin can only be removed when CS2 is closed.",
                                            "Close CS2 and try again.")
                    Cs2(config.load().cs2_path, PLUGIN_SO, WORK_DIR / "tmp/raw").uninstall()
                    video_settings().restore()
                    return self._json({"ok": True})
                self._json({"error": {"title": "Not found", "why": url.path, "fix": ""}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

    return Handler


INSTANCE_PATH = WORK_DIR / "gui.json"


def _open_window(url: str) -> None:
    browser = _find_app_browser()
    if browser:
        profile = WORK_DIR / "gui-profile"
        detach = {} if system.IS_WINDOWS else {"start_new_session": True}
        subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,860",
                          "--class=cs2hl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
    else:
        webbrowser.open(url)


def _running_instance() -> str | None:
    """URL of an already running app, so a second launch just opens another window onto the same queue."""
    try:
        info = json.loads(INSTANCE_PATH.read_text())
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/queue", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2):
            return f"http://127.0.0.1:{info['port']}/#token={info['token']}"
    except (OSError, ValueError, KeyError):
        return None


def serve(open_window: bool = True, port: int = 0) -> None:
    if open_window and (existing := _running_instance()):
        print("CS2 Highlight Maker is already running; opening a window.", flush=True)
        _open_window(existing)
        return
    firstrun.provision_plugin()
    app = App()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/#token={app.token}"
    fd = os.open(INSTANCE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": app.token, "pid": os.getpid()}, f)
    print(f"CS2 Highlight Maker running at {url}", flush=True)
    print("Close the window to quit (it keeps running while the queue has work).", flush=True)
    if open_window:
        _open_window(url)

    def idle_watch():
        while True:
            time.sleep(10)
            if not app.runner.busy and time.time() - app.last_seen > IDLE_EXIT_SECONDS:
                httpd.shutdown()
                return

    if open_window:
        threading.Thread(target=idle_watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        INSTANCE_PATH.unlink(missing_ok=True)
