"""Local web GUI: a small JSON API on 127.0.0.1 plus static files, opened in an app-style browser window.

Every API call must carry the random per-launch token, so other websites open in the browser cannot drive it.
"""

from __future__ import annotations

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
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import segno

from .. import config, jobs, pipeline, steam
from ..config import MODES, PLUGIN_SO, STEAM_ROOT, WORK_DIR
from ..errors import FriendlyError, explain
from ..game import CROSSHAIRS, Cs2

STATIC = Path(__file__).parent / "static"
IDLE_EXIT_SECONDS = 90  # quit when no window has polled for this long and nothing is running
APP_BROWSERS = ("brave", "brave-browser", "chromium", "chromium-browser", "google-chrome-stable",
                "google-chrome", "microsoft-edge", "vivaldi")


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(18)
        self.runner = jobs.JobRunner()
        self.login: steam.QrLogin | steam.PasswordLogin | None = None
        self.login_error: dict | None = None
        self.matches_cache: tuple[float, list[steam.Match]] | None = None
        self.last_seen = time.time()

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
        recent = sorted(self.runner.jobs.values(), key=lambda j: j.id, reverse=True)[:10]
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
            "work_dir": str(WORK_DIR.resolve()),
            "plugin_ok": PLUGIN_SO.exists(),
            "steam_running": pipeline.steam_running(),
            "cs2_running": Cs2.is_running(),
            "current_job": current.id if current else None,
            "jobs": [{"id": j.id, "title": j.title, "status": j.status} for j in recent],
        }

    def save_config(self, changes: dict) -> dict:
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
        return self.state()

    def matches(self, refresh: bool, pages: int) -> list[dict]:
        session = steam.Session.load()
        if session is None:
            raise FriendlyError("Not logged in to Steam", "The match list comes from your Steam account.",
                                "Open the Steam tab and log in.")
        if refresh or not self.matches_cache or time.time() - self.matches_cache[0] > 120:
            self.matches_cache = (time.time(), steam.match_history(session, per_tab_pages=pages))
        done = {k.split(".dem:")[0] for k, v in pipeline.load_state().items() if v.get("files")}
        return [{**m.__dict__, "rendered": m.id in done} for m in self.matches_cache[1]]

    def local_demos(self) -> list[dict]:
        cfg = config.load()
        state = pipeline.load_state()
        out = []
        for dem in sorted(cfg.replays_dir.glob("*.dem"), key=lambda p: p.stat().st_mtime, reverse=True):
            info = state.get(pipeline.demo_key(dem), {})
            out.append({"path": str(dem), "name": dem.name, "size_mb": round(dem.stat().st_size / 1e6),
                        "modified": datetime.fromtimestamp(dem.stat().st_mtime).isoformat(timespec="minutes"),
                        "rendered": bool(info.get("files")), "skipped": info.get("skipped")})
        return out

    def outputs(self) -> list[dict]:
        cfg = config.load()
        items = []
        for folder in (cfg.montages_dir, cfg.clips_dir):
            if folder.exists():
                for f in folder.rglob("*.mp4"):
                    items.append({"path": str(f), "name": f.name, "group": f.parent.name,
                                  "kind": "montage" if folder == cfg.montages_dir else "clip",
                                  "size_mb": round(f.stat().st_size / 1e6), "mtime": f.stat().st_mtime})
        return sorted(items, key=lambda i: i["mtime"], reverse=True)[:200]

    @staticmethod
    def browse(path: str) -> dict:
        p = Path(path).expanduser() if path else Path.home()
        if not p.is_dir():
            p = p.parent if p.parent.is_dir() else Path.home()
        dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
        roots = [str(Path.home())] + [str(m) for m in Path("/run/media", os.environ.get("USER", "")).glob("*")
                                      if m.is_dir()] + ["/mnt", "/media"]
        return {"path": str(p), "parent": str(p.parent), "dirs": dirs[:500],
                "is_cs2": (p / "game/cs2.sh").exists(), "roots": [r for r in roots if Path(r).is_dir()]}

    @staticmethod
    def open_path(path: str, reveal: bool) -> None:
        p = Path(path)
        target = p.parent if reveal and p.is_file() else p
        if not target.exists():
            raise FriendlyError("File not found", str(p), "It may have been moved or deleted.")
        subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

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
            self.login, self.matches_cache = None, None
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


def _find_app_browser() -> str | None:
    for name in APP_BROWSERS:
        if path := shutil.which(name):
            return path
    return None


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
                    return self._json(app.matches(q.get("refresh") == "1", int(q.get("pages", 1))))
                if url.path == "/api/local":
                    return self._json(app.local_demos())
                if url.path == "/api/outputs":
                    return self._json(app.outputs())
                if url.path == "/api/browse":
                    return self._json(app.browse(q.get("path", "")))
                if url.path == "/api/login/status":
                    return self._json(app.login_status())
                if m := re.fullmatch(r"/api/jobs/(\d+)", url.path):
                    job = app.runner.jobs.get(int(m.group(1)))
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
                if url.path == "/api/jobs":
                    return self._json(self._submit(body))
                if m := re.fullmatch(r"/api/jobs/(\d+)/cancel", url.path):
                    app.runner.cancel(int(m.group(1)))
                    return self._json({"ok": True})
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
                    app.matches_cache = None
                    return self._json({"ok": True})
                if url.path == "/api/open":
                    app.open_path(body["path"], bool(body.get("reveal")))
                    return self._json({"ok": True})
                if url.path == "/api/clean":
                    if Cs2.is_running():
                        raise FriendlyError("CS2 is running", "The plugin can only be removed when CS2 is closed.",
                                            "Close CS2 and try again.")
                    Cs2(config.load().cs2_path, PLUGIN_SO, WORK_DIR / "tmp/raw").uninstall()
                    return self._json({"ok": True})
                self._json({"error": {"title": "Not found", "why": url.path, "fix": ""}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def _submit(self, body: dict) -> dict:
            kind, mode = body.get("kind"), body.get("mode")
            if mode is not None and mode not in MODES:
                raise FriendlyError("Unknown video mode", mode, "Pick a mode in Settings.")
            if kind == "latest":
                job = app.runner.submit("Latest match", jobs.render_match(None, mode))
            elif kind == "match":
                match = steam.Match(**body["match"])
                job = app.runner.submit(f"{match.mode} {match.map} ({match.time[:10]})",
                                        jobs.render_match(match, mode))
            elif kind == "local":
                demo = Path(body["path"])
                if not demo.is_file():
                    raise FriendlyError("Demo not found", str(demo), "Refresh the list.")
                job = app.runner.submit(demo.name, jobs.render_local(demo, mode))
            else:
                raise FriendlyError("Unknown job", str(kind), "Reload the window.")
            return {"id": job.id}

    return Handler


def serve(open_window: bool = True, port: int = 0) -> None:
    app = App()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#token={app.token}"
    print(f"CS2 Highlight Maker running at {url}", flush=True)
    print("Close the window to quit (the server stops shortly after, unless a render is running).")
    if open_window:
        browser = _find_app_browser()
        if browser:
            profile = WORK_DIR / "gui-profile"
            subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,860",
                              "--class=cs2hl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        else:
            webbrowser.open(url)

    def idle_watch():
        while True:
            time.sleep(10)
            busy = app.runner.current is not None or any(j.status == "queued" for j in app.runner.jobs.values())
            if not busy and time.time() - app.last_seen > IDLE_EXIT_SECONDS:
                httpd.shutdown()
                return

    if open_window:
        threading.Thread(target=idle_watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
