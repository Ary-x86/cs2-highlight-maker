"""Steam login (QR code or password + Steam Guard) and CS2 match history / replay downloads.

Match history comes from the "Personal Game Data" pages on steamcommunity.com
(profiles/<id>/gcpd/730?tab=matchhistory...), the same pages that show the "Download Replay" buttons. Those links
point at Valve's replay servers over plain HTTP; the files are the official .dem.bz2 replays.
"""

from __future__ import annotations

import base64
import bz2
import html
import http.cookiejar
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import DOWNLOADS_DIR, SESSION_PATH
from .errors import FriendlyError

API = "https://api.steampowered.com/IAuthenticationService"
COMMUNITY = "https://steamcommunity.com"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) cs2-highlight-maker"
PLATFORM_WEB_BROWSER = 2
GUARD_EMAIL, GUARD_DEVICE_CODE, GUARD_DEVICE_CONFIRM = 2, 3, 4

MATCH_TABS = {
    "matchhistorypremier": "Premier",
    "matchhistorycompetitive": "Competitive",
    "matchhistorycompetitivepermap": "Ranked Competitive",
    "matchhistorywingman": "Wingman",
}


# --- HTTP helpers -----------------------------------------------------------------------------------

def _request(url: str, data: dict | None = None, headers: dict | None = None, opener=None, timeout=30):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        return (opener or urllib.request.build_opener()).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise FriendlyError("Steam is rate-limiting us", "Too many requests in a short time.",
                                "Wait a few minutes and try again.") from e
        raise
    except urllib.error.URLError as e:
        raise FriendlyError("Cannot reach Steam", str(e.reason),
                            "Check your internet connection and try again.") from e


def _api(method: str, data: dict | None = None, get: bool = False) -> dict:
    url = f"{API}/{method}/v1/"
    if get:
        url += "?" + urllib.parse.urlencode(data or {})
        data = None
    with _request(url, data=data) as resp:
        return json.loads(resp.read().decode()).get("response", {})


def _jwt_payload(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# --- session --------------------------------------------------------------------------------------

@dataclass
class Session:
    steamid: str
    account_name: str
    refresh_token: str
    access_token: str = ""

    def save(self) -> None:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(SESSION_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(self), f)

    @staticmethod
    def load() -> Session | None:
        try:
            return Session(**json.loads(SESSION_PATH.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def logout() -> None:
        SESSION_PATH.unlink(missing_ok=True)

    def _access_token_valid(self) -> bool:
        if not self.access_token:
            return False
        try:
            return _jwt_payload(self.access_token).get("exp", 0) > time.time() + 300
        except (ValueError, IndexError):
            return False

    def refresh(self) -> None:
        if self._access_token_valid():
            return
        if _jwt_payload(self.refresh_token).get("exp", 0) < time.time():
            raise FriendlyError("Steam login expired", "Saved Steam logins last about 200 days.",
                                "Open the Steam tab and log in again.")
        resp = _api("GenerateAccessTokenForApp", {"refresh_token": self.refresh_token, "steamid": self.steamid})
        if not resp.get("access_token"):
            raise FriendlyError("Steam login is no longer valid",
                                "Steam rejected the saved login (password change or 'deauthorize devices').",
                                "Open the Steam tab and log in again.")
        self.access_token = resp["access_token"]
        if resp.get("refresh_token"):
            self.refresh_token = resp["refresh_token"]
        self.save()

    def opener(self):
        """URL opener carrying steamcommunity.com login cookies."""
        self.refresh()
        jar = http.cookiejar.CookieJar()
        for name, value in (("steamLoginSecure", f"{self.steamid}%7C%7C{self.access_token}"),
                            ("sessionid", secrets.token_hex(12)), ("Steam_Language", "english")):
            jar.set_cookie(http.cookiejar.Cookie(
                0, name, value, None, False, "steamcommunity.com", True, False, "/", True, True,
                None, False, None, None, {}))
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _finish(resp: dict) -> Session | None:
    if not resp.get("refresh_token"):
        return None
    steamid = _jwt_payload(resp["refresh_token"])["sub"]
    session = Session(steamid, resp.get("account_name", ""), resp["refresh_token"], resp.get("access_token", ""))
    session.save()
    return session


# --- QR login ---------------------------------------------------------------------------------------

class QrLogin:
    """Scan challenge_url with the Steam mobile app, then call poll() until it returns a Session."""

    def __init__(self):
        resp = _api("BeginAuthSessionViaQR", {"device_friendly_name": "CS2 Highlight Maker",
                                              "platform_type": PLATFORM_WEB_BROWSER})
        self.client_id = resp["client_id"]
        self.request_id = resp["request_id"]
        self.challenge_url = resp["challenge_url"]
        self.interval = float(resp.get("interval", 5))
        self.started = time.time()

    def poll(self) -> Session | None:
        resp = _api("PollAuthSessionStatus", {"client_id": self.client_id, "request_id": self.request_id})
        if resp.get("new_challenge_url"):
            self.challenge_url = resp["new_challenge_url"]
        if resp.get("new_client_id"):
            self.client_id = resp["new_client_id"]
        return _finish(resp)


# --- password login -----------------------------------------------------------------------------------

def _rsa_encrypt(message: bytes, modulus_hex: str, exponent_hex: str) -> str:
    """RSA PKCS#1 v1.5 encryption, as Steam's login expects."""
    n, e = int(modulus_hex, 16), int(exponent_hex, 16)
    k = (n.bit_length() + 7) // 8
    pad_len = k - len(message) - 3
    padding = bytes(secrets.choice(range(1, 256)) for _ in range(pad_len))
    block = b"\x00\x02" + padding + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), e, n).to_bytes(k, "big")
    return base64.b64encode(cipher).decode()


class PasswordLogin:
    """Begin with account name + password; then either submit_code() or wait for app approval via poll()."""

    def __init__(self, account_name: str, password: str):
        key = _api("GetPasswordRSAPublicKey", {"account_name": account_name}, get=True)
        if not key.get("publickey_mod"):
            raise FriendlyError("Unknown Steam account", f"Steam does not know the account '{account_name}'.",
                                "Use your Steam *login* name (not your display name).")
        try:
            resp = _api("BeginAuthSessionViaCredentials", {
                "account_name": account_name,
                "encrypted_password": _rsa_encrypt(password.encode(), key["publickey_mod"], key["publickey_exp"]),
                "encryption_timestamp": key["timestamp"],
                "remember_login": "true",
                "platform_type": PLATFORM_WEB_BROWSER,
                "persistence": 1,
                "website_id": "Community",
                "device_friendly_name": "CS2 Highlight Maker",
            })
        except urllib.error.HTTPError as e:
            raise FriendlyError("Steam refused the login", f"HTTP {e.code}",
                                "Check the account name and password.") from e
        if not resp.get("client_id"):
            raise FriendlyError("Wrong account name or password", "Steam rejected the credentials.",
                                "Check them and try again. Too many attempts get you rate-limited for a while.")
        self.client_id = resp["client_id"]
        self.request_id = resp["request_id"]
        self.steamid = resp["steamid"]
        self.guard = [c["confirmation_type"] for c in resp.get("allowed_confirmations", [])]

    @property
    def needs_code(self) -> str | None:
        if GUARD_DEVICE_CODE in self.guard:
            return "app"
        if GUARD_EMAIL in self.guard:
            return "email"
        return None

    def submit_code(self, code: str) -> None:
        code_type = GUARD_DEVICE_CODE if self.needs_code == "app" else GUARD_EMAIL
        try:
            _api("UpdateAuthSessionWithSteamGuardCode", {"client_id": self.client_id, "steamid": self.steamid,
                                                         "code": code.strip().upper(), "code_type": code_type})
        except urllib.error.HTTPError as e:
            raise FriendlyError("Steam Guard code rejected", "The code was wrong or expired.",
                                "Enter the newest code from the Steam app or your e-mail.") from e

    def poll(self) -> Session | None:
        return _finish(_api("PollAuthSessionStatus", {"client_id": self.client_id, "request_id": self.request_id}))


# --- match history ------------------------------------------------------------------------------------

@dataclass
class Match:
    id: str            # replay file stem, e.g. 003847966708505509917_1556420564
    mode: str          # tab label
    map: str
    time: str          # ISO 8601 UTC
    duration: str
    url: str           # .dem.bz2 download link, "" when the replay has expired
    score: str         # "13 : 8" from your team's point of view
    result: str        # win / loss / tie
    kills: int
    assists: int
    deaths: int


_ROW_SPLIT = re.compile(r'<td class="val_left">')
_TEXT = re.compile(r"<[^>]+>")


def _cells(fragment: str) -> list[str]:
    return [html.unescape(_TEXT.sub("", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", fragment, re.S)]


def parse_matches(page: str, mode: str, steamid: str) -> list[Match]:
    account_id = str(int(steamid) - 76561197960265728)
    matches = []
    for block in _ROW_SPLIT.split(page)[1:]:
        left, _, right = block.partition('<table class="csgo_scoreboard_inner_right">')
        left_cells = [c for c in _cells(left) if c]
        # First cell is e.g. "Competitive Dust II" / "Premier Ancient"
        map_name = re.sub(r"^(Competitive|Premier|Wingman|Scrimmage)\s+", "", left_cells[0]) if left_cells else "?"
        when = next((c for c in left_cells if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", c)), "")
        duration = next((c.split(":", 1)[1].strip() for c in left_cells if c.startswith("Match Duration")), "")
        url_m = re.search(r'href="(https?://replay[^"]+\.dem\.bz2)"', left)
        url = url_m.group(1) if url_m else ""
        match_id = url.rsplit("/", 1)[-1].removesuffix(".dem.bz2") if url else ""

        # Scoreboard: team A rows, then the score row, then team B rows.
        score_m = re.search(r'csgo_scoreboard_score">\s*(\d+)\s*:\s*(\d+)', right)
        me_m = re.search(rf'data-miniprofile="{account_id}"', right)
        kills = assists = deaths = 0
        score, result = "", ""
        if me_m:
            row_start = right.rfind("<tr>", 0, me_m.start())
            row_end = right.find("</tr>", me_m.start())
            nums = [c for c in _cells(right[row_start:row_end]) if re.fullmatch(r"-?\d+", c)]
            if len(nums) >= 4:  # ping, K, A, D
                kills, assists, deaths = int(nums[1]), int(nums[2]), int(nums[3])
            if score_m:
                a, b = int(score_m.group(1)), int(score_m.group(2))
                mine, theirs = (a, b) if me_m.start() < score_m.start() else (b, a)
                score = f"{mine} : {theirs}"
                result = "win" if mine > theirs else "loss" if mine < theirs else "tie"
        try:
            iso = datetime.strptime(when.replace(" GMT", ""), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            iso = ""
        matches.append(Match(match_id or f"{mode}-{when}", mode, map_name, iso, duration, url, score, result,
                             kills, assists, deaths))
    return matches


def match_history(session: Session, per_tab_pages: int = 1) -> list[Match]:
    """Newest first, across Premier / Competitive / Wingman."""
    opener = session.opener()
    found: dict[str, Match] = {}
    for tab, label in MATCH_TABS.items():
        url = f"{COMMUNITY}/profiles/{session.steamid}/gcpd/730?tab={tab}"
        with _request(url, opener=opener) as resp:
            page = resp.read().decode("utf-8", "replace")
            final_url = resp.geturl()
        if "/login" in final_url or "g_steamID = false" in page:
            raise FriendlyError("Steam login is no longer valid",
                                "steamcommunity.com did not accept the saved login.",
                                "Open the Steam tab and log in again.")
        token = re.search(r"g_sGcContinueToken = '(\d+)'", page)
        session_id = re.search(r'g_sessionID = "([0-9a-f]+)"', page)
        for m in parse_matches(page, label, session.steamid):
            found.setdefault(m.id, m)
        for _ in range(per_tab_pages - 1):
            if not (token and session_id):
                break
            more = f"{COMMUNITY}/profiles/{session.steamid}/gcpd/730?" + urllib.parse.urlencode(
                {"ajax": 1, "tab": tab, "continue_token": token.group(1), "sessionid": session_id.group(1)})
            with _request(more, opener=opener) as resp:
                data = json.loads(resp.read().decode())
            for m in parse_matches(data.get("html", ""), label, session.steamid):
                found.setdefault(m.id, m)
            token = re.match(r"(\d+)", str(data.get("continue_token") or ""))
    return sorted(found.values(), key=lambda m: m.time, reverse=True)


# --- download -----------------------------------------------------------------------------------------

def download(match: Match, progress=None) -> Path:
    """Fetch and unpack a replay into the downloads folder. progress(fraction, detail) is optional."""
    if not match.url:
        raise FriendlyError("Replay no longer available",
                            "Valve only keeps match replays for a few weeks.",
                            "Pick a more recent match.")
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dem = DOWNLOADS_DIR / f"{match.id}.dem"
    if dem.exists():
        return dem
    part = dem.with_suffix(".dem.part")
    try:
        resp = _request(match.url, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FriendlyError("Replay no longer available", "Valve already deleted this replay (HTTP 404).",
                                "Pick a more recent match.") from e
        raise FriendlyError("Download failed", f"Valve's replay server answered HTTP {e.code}.",
                            "Try again in a minute.") from e
    total = int(resp.headers.get("Content-Length") or 0)
    got = 0
    started = time.monotonic()
    decomp = bz2.BZ2Decompressor()
    try:
        with resp, open(part, "wb") as out:
            while chunk := resp.read(1 << 20):
                got += len(chunk)
                out.write(decomp.decompress(chunk))
                if progress:
                    speed = got / max(1e-6, time.monotonic() - started)
                    detail = f"{got / 1e6:.0f}/{total / 1e6:.0f} MB at {speed / 1e6:.1f} MB/s" if total \
                        else f"{got / 1e6:.0f} MB"
                    progress(got / total if total else None, detail)
    except (OSError, EOFError) as e:
        part.unlink(missing_ok=True)
        raise FriendlyError("Download failed", f"{type(e).__name__}: {e}",
                            "Check your connection and disk space, then try again.") from e
    part.replace(dem)
    return dem
