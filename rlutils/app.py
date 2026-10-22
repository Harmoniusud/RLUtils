"""Desktop app: the MMR-over-time chart (fed live by Rocket League's servers after an Epic sign-in, by the game's
log, and by a one-time import of tracker.gg's history), the Games tracker (fed by Rocket League's Stats API),
launching Rocket League on a chosen Epic account, settings, an update check and start with Windows.
"""
from __future__ import annotations

import csv
import getpass
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMainWindow, QMenu, QSystemTrayIcon,
)

from . import data, game, importer, psynet, stats, ui, vibrance, viewmodel
from .data import (
    APP_NAME, DATA_DIR, DB_PATH, FROZEN, LIVE_PLAYLISTS, ROOT, THRESHOLDS_PATH, TRACKED_PLAYLISTS, VERSION,
    load_config, load_thresholds, playlist_name, rank_label, tier_distance, rl_shortcut_path, save_config,
    switcher_path, switcher_store,
)
from .ui import Background

UI_NAME = APP_NAME


# ====================================================================================================
# Autostart: Start with Windows: one value under HKCU\Software\Microsoft\Windows\CurrentVersion\Run.
#
# Changed only when the user flips the setting in the app; no admin rights needed.
# ====================================================================================================


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "RLUtils"
OLD_VALUE = "RLTracker"  # an older name of the app, removed when the setting changes


def command() -> str:
    if FROZEN:
        return f'"{sys.executable}" --tray'
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return f'"{windowless if windowless.exists() else exe}" "{ROOT / "rlutils.py"}" --tray'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        for name in ((OLD_VALUE,) if on else (VALUE, OLD_VALUE)):
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())


# ====================================================================================================
# App: the MMR chart, Games, Play (Rocket League on a chosen Epic account) and Settings.
# ====================================================================================================




RELEASES_API = "https://api.github.com/repos/Harmoniusud/RLUtils/releases/latest"


def version_tuple(text: str) -> tuple[int, ...]:
    """'v0.11.1' -> (0, 11, 1); () when it isn't a version."""
    parts = str(text or "").strip().lstrip("vV").split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) and parts != [""] else ()


def installed_copy() -> bool:
    """Running from the installer's install (its uninstaller sits next to the exe): updates can install."""
    return FROZEN and (Path(sys.executable).parent / "unins000.exe").exists()


def installer_asset(release: dict) -> dict | None:
    """The release's RLUtils-Setup-<v>.exe with the SHA-256 GitHub computed for it, or None."""
    for a in release.get("assets") or []:
        name, digest = str(a.get("name") or ""), str(a.get("digest") or "")
        if name.lower().startswith("rlutils-setup-") and name.lower().endswith(".exe") and digest.startswith("sha256:"):
            return {"name": name, "url": a.get("browser_download_url") or "", "size": int(a.get("size") or 0),
                    "sha256": digest.split(":", 1)[1].lower()}
    return None


def download_verified(url: str, dest: Path, sha256: str, size: int, progress=None) -> Path:
    """Download to `dest` and check its SHA-256; a file that doesn't match is deleted and refused."""
    if not url.startswith("https://github.com/Harmoniusud/RLUtils/releases/download/"):
        raise ValueError("unexpected download address")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": f"RLUtils/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and size:
                progress(min(99, done * 100 // size))
    if h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise ValueError("the download doesn't match the checksum GitHub published for it; nothing was installed")
    part.replace(dest)
    return dest


class UpdateChecker(QObject):
    """Asks GitHub for the newest RLUtils release when the app starts and once a day. When the user clicks
    Update (installed copies only), downloads that release's installer from GitHub, checks it against the
    SHA-256 GitHub publishes for it, runs it silently and quits; the installer starts the new version.
    Nothing about this PC is sent."""

    found = Signal()

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self.bg = Background(self)
        self.latest: dict | None = None   # {"version", "url", "asset"} of a release newer than this one
        self.state = ("idle", "")         # idle, downloading (percent), installing, error (text)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)

    def install(self) -> None:
        u = self.latest
        if not u or self.state[0] in ("downloading", "installing"):
            return
        asset = u.get("asset")
        if not (installed_copy() and asset):
            QDesktopServices.openUrl(QUrl(u["url"]))  # a portable or source copy: the release page
            return
        dest = Path(tempfile.gettempdir()) / "RLUtils-update" / asset["name"]
        self.state = ("downloading", "0")
        self.found.emit()

        def progress(pct: int) -> None:
            self.state = ("downloading", str(pct))

        def done(path, err) -> None:
            if err:
                self.state = ("error", f"Update failed: {err}")
                self.found.emit()
                return
            self.state = ("installing", "")
            self.found.emit()
            # the installer starts once this app has fully exited (its files must not be in use), then starts
            # the new version itself (/restart=1, see RLUtils.iss)
            script = (f"Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue; "
                      "Start-Sleep -Milliseconds 500; "
                      f"Start-Process -FilePath '{str(path).replace(chr(39), chr(39) * 2)}' -ArgumentList "
                      "'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/restart=1'")
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
                              script], close_fds=True, creationflags=game.CREATE_NO_WINDOW)
            QTimer.singleShot(300, self.window.quit_app)

        self.bg.run(lambda: download_verified(asset["url"], dest, asset["sha256"], asset["size"], progress), done)

    def start(self) -> None:
        self.timer.start(24 * 3600 * 1000)
        QTimer.singleShot(10_000, self.check)

    def check(self) -> None:
        if not self.window.cfg["app"].get("check_updates", True):
            return

        def work():
            req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": f"RLUtils/{VERSION}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())

        self.bg.run(work, self._done)

    def _done(self, release, err) -> None:
        if err or not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            return  # offline or GitHub unreachable: try again tomorrow
        tag = str(release.get("tag_name") or "")
        if version_tuple(tag) > version_tuple(VERSION):
            self.latest = {"version": tag.lstrip("vV"),
                           "url": release.get("html_url") or "https://github.com/Harmoniusud/RLUtils/releases",
                           "asset": installer_asset(release)}
            self.found.emit()


PRIVACY_TEXT = ("Your Epic login stays on this PC. You sign in on Epic's own website in your browser, so RLUtils "
                "never sees or stores your password. What it keeps is Epic's sign-in for this PC, encrypted with "
                "Windows so only your Windows user on this PC can read it. It is only ever sent to Epic and Rocket "
                "League's own servers, to read your MMR. RLUtils has no server of its own and nobody else, "
                "including the developer, can ever access your login.")


class EpicSignIn(QObject):
    """Epic's device sign-in: open epicgames.com/activate (code filled in) in the user's browser, then ask Epic
    every few seconds whether they approved it. The result is saved in the LoginStore."""

    changed = Signal()
    signed_in = Signal(str, str)    # account id, Epic name

    def __init__(self, parent, store: psynet.LoginStore):
        super().__init__(parent)
        self.store = store
        self.bg = Background(self)
        self.login: psynet.DeviceLogin | None = None
        self.state = "idle"   # idle, starting, waiting, saving, error
        self.error = ""
        self.busy = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)

    def start(self) -> None:
        if self.state in ("starting", "waiting", "saving"):
            self.open_page()
            return
        self.state, self.error, self.login = "starting", "", None
        self.changed.emit()

        def done(login, err):
            if self.state != "starting":
                return  # cancelled meanwhile
            if err:
                self.state, self.error = "error", f"Couldn't reach Epic: {err}"
            else:
                self.login, self.state = login, "waiting"
                self.open_page()
                self.timer.start(login.interval * 1000)
            self.changed.emit()

        self.bg.run(psynet.start_device_login, done)

    def open_page(self) -> None:
        if self.login:
            QDesktopServices.openUrl(QUrl(self.login.url))

    def cancel(self) -> None:
        self.timer.stop()
        self.state, self.login, self.error = "idle", None, ""
        self.changed.emit()

    def _poll(self) -> None:
        login = self.login
        if self.busy or not login:
            return
        if time.monotonic() > login.expires_at:
            self.timer.stop()
            self.state, self.error = "error", "The sign-in code expired. Start again."
            self.changed.emit()
            return
        self.busy = True

        def done(res, err):
            self.busy = False
            if self.login is not login:
                return
            if err:
                self.timer.stop()
                self.state, self.error = "error", f"Couldn't finish signing in: {err}"
            elif res:
                self.timer.stop()
                account, name, creds = res
                self.store.put(account, name, **creds)
                self.state, self.login = "idle", None
                self.signed_in.emit(account, name)
            self.changed.emit()

        self.bg.run(lambda: psynet.poll_device_login(login), done)

    def view(self) -> dict | None:
        if self.state == "idle":
            return None
        return {"state": self.state, "code": self.login.user_code if self.login else "",
                "url": self.login.url if self.login else "", "error": self.error}


class PsyNetWatcher(QObject):
    """Live MMR from Rocket League's servers (psynet.py) for every Epic account signed in here: when the app
    starts, when Rocket League closes (so the last match counts), and every `psynet.every_min` minutes while
    it isn't running. Never while it runs: signing into PsyNet beside the game could drop the game's session."""

    saved = Signal(str, int)    # profile key, new readings
    status = Signal()

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self.bg = Background(self)
        self.store = psynet.LoginStore(DATA_DIR / "epic_login.dat")
        self.version = psynet.Version(DATA_DIR / "psynet_version.json")
        self.busy = False
        self.state = ("wait", "Starting")
        self.last_at: datetime | None = None
        self.expired: set[str] = set()   # accounts whose sign-in stopped working: asked to sign in again
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def accounts(self) -> list[tuple[str, str]]:
        return self.store.accounts()

    def start(self) -> None:
        if data.drop_raw_psynet_readings():
            self.window.refresh(push=False)
        self.timer.start(max(5, int(self.window.cfg["psynet"].get("every_min", 30))) * 60_000)
        QTimer.singleShot(3000, self.tick)

    def _status(self, state: str, text: str) -> None:
        if (state, text) != self.state:
            self.state = (state, text)
            self.status.emit()

    def tick(self) -> None:
        if self.busy:
            return
        if not self.accounts():
            self._status("off", "Not signed in to Epic")
            return
        if self.window._rl_running:
            self._status("paused", "Paused while Rocket League runs; updates when you close it")
            return
        self.busy = True
        self._status("busy", "Reading MMR from Rocket League's servers…")
        self.bg.run(self._work, self._done)

    def _work(self):
        results, errors, expired = [], [], []
        for account, name in self.accounts():
            try:
                f = psynet.fetch_skills(self.store, account, self.version)
                results.append(psynet.save(f, LIVE_PLAYLISTS))
            except psynet.LoginExpired:
                expired.append((account, name))
            except Exception as exc:  # one account failing must not stop the others
                errors.append(f"{name or account[:6]}: {exc}")
        return results, errors, expired

    def _done(self, res, err) -> None:
        self.busy = False
        if err:
            self._status("error", str(err))
            return
        results, errors, expired = res
        self.last_at = datetime.now(timezone.utc)
        for key, added in results:
            if added:
                self.saved.emit(key, added)
        if errors:
            self._status("error", "; ".join(errors))
        else:
            self._status("ok", f"Live from Rocket League's servers · checked {self.last_at.astimezone():%H:%M}")
        for account, name in expired:
            if account not in self.expired:
                self.expired.add(account)
                self.window.ask_epic_login(again=name or account[:6])


class VibranceWatcher(QObject):
    """Every second: while Rocket League is the foreground window, set NVIDIA Digital Vibrance on its monitor
    to vibrance.pct, and put back the level it had otherwise. Keeps the OBS Color Correction filter on the
    Rocket League capture in step (retried every 15 s while OBS isn't reachable), so recordings show it too."""

    status = Signal(str)

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self.bg = Background(self)
        try:
            self.nv, self.nv_error = vibrance.Nvapi(), ""
        except vibrance.VibranceError as exc:
            self.nv, self.nv_error = None, str(exc)
        self.applied: dict[int, int] = {}  # display handle -> level to put back
        self.screen_state = ""
        self.obs_state = ""
        self.obs_synced: float | None = None
        self.obs_busy = False
        self.obs_retry_at = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def start(self) -> None:
        self.timer.start(1000)
        self.tick()

    def _emit(self) -> None:
        self.status.emit(self.screen_state + ("<br>" + self.obs_state if self.obs_state else ""))

    def tick(self) -> None:
        cfg = self.window.cfg["vibrance"]
        pct = int(cfg["pct"])
        screen = self.nv_error and f"Screen: {self.nv_error}"
        if self.nv:
            try:
                hwnd = vibrance.foreground_window()
                in_rl = bool(hwnd) and game._process_name(vibrance.window_pid(hwnd)) == "rocketleague.exe"
                if in_rl and pct > 50:
                    disp = self.nv.display_for(vibrance.monitor_device(hwnd)) or next(iter(self.nv.displays()), None)
                    if disp:
                        cur, lo, hi = self.nv.info(disp)
                        want = vibrance.pct_to_level(pct, lo, hi)
                        self.applied.setdefault(disp, cur)
                        if cur != want:
                            self.nv.set_level(disp, want)
                    screen = f"Screen: {pct}% while Rocket League is in front (now)"
                else:
                    self.restore()
                    screen = ("Screen: driver default (slider at 50%)" if pct <= 50
                              else f"Screen: {pct}% once Rocket League is in front")
            except vibrance.VibranceError as exc:
                screen = f"Screen: {exc}"
        if screen != self.screen_state:
            self.screen_state = screen
            self._emit()
        self._sync_obs(cfg, pct)

    def restore(self) -> None:
        for disp, level in list(self.applied.items()):
            try:
                self.nv.set_level(disp, level)
            except vibrance.VibranceError:
                pass
        self.applied.clear()

    def _sync_obs(self, cfg: dict, pct: int) -> None:
        want = vibrance.pct_to_obs_saturation(pct, float(cfg["obs_strength"])) if cfg["obs"] else 0.0
        if self.obs_busy or want == self.obs_synced or time.monotonic() < self.obs_retry_at:
            return
        self.obs_busy = True
        url, password = cfg["obs_url"], cfg["obs_password"]

        def work():
            obs = vibrance.Obs(url, password)
            try:
                sources = obs.rl_sources()
                for name in sources:
                    obs.set_saturation(name, want)
                return sources
            finally:
                obs.close()

        def done(sources, err):
            self.obs_busy = False
            if err:
                self.obs_retry_at = time.monotonic() + 15
                reason = "not running" if isinstance(err, OSError) else str(err)
                self.obs_state = f"Recordings: OBS {reason}; retrying"
            elif not sources:
                self.obs_retry_at = time.monotonic() + 15
                self.obs_state = "Recordings: OBS has no Game/Window Capture of RocketLeague.exe"
            else:
                self.obs_synced = want
                names = ", ".join(sources)
                self.obs_state = (f"Recordings: OBS filter '{vibrance.OBS_FILTER}' on {names}, saturation {want:+.2f}"
                                  if want > 0 else f"Recordings: OBS filter off on {names}")
            self._emit()

        self.bg.run(work, done)


class GamesWatcher(QObject):
    """Follows Rocket League's Stats API on a background thread (stats.run_reader) and saves every finished
    match: to the profile of your account in it (added the first time). Only listens; never sends to the game."""

    saved = Signal(str, str)   # profile key, one-line summary
    status = Signal(str, str)  # state ("ok", "live", "wait", "error", "off"), text

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.feed = stats.GameFeed(lambda rec: self.events.put(("game", rec)),
                                   log=lambda line: self.events.put(("log", line)),
                                   on_rate=lambda line: self.events.put(("rate", line)))
        self.link = "waiting"
        self.last = ""  # last saved / skipped line
        self.state = ("off", "Game stats off")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain)

    def known_ids(self) -> frozenset:
        ids = {stats.strip_split(p["player_id"]) for p in data.list_profiles() if p["player_id"]}
        account = self.window.cfg["launch"].get("account")
        if account:
            ids.add(stats.strip_split(data.epic_player_id(account)))
        return frozenset(i for i in ids if i)

    def start(self) -> None:
        cfg = self.window.cfg["games"]
        if not cfg.get("enabled", True):
            self._status("off", "Game stats are off (games.enabled in config.json)")
            return
        self.feed.known_ids = self.known_ids()
        self.feed.save_offline = bool(cfg.get("save_offline"))
        self.thread = threading.Thread(
            target=stats.run_reader, name="statsapi", daemon=True,
            args=(self.feed, cfg["host"], int(cfg["web_port"]), int(cfg["port"]), self.stop_event, self.events),
            kwargs={"is_game_running": lambda: game.process_running("RocketLeague.exe")})
        self.thread.start()
        self.timer.start(500)
        self._status("wait", "Waiting for Rocket League's Stats API")

    def stop(self) -> None:
        self.stop_event.set()

    def _status(self, state: str, text: str) -> None:
        if (state, text) != self.state:
            self.state = (state, text)
            self.status.emit(state, text)

    def _drain(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.link = value
                if value != "waiting":
                    self.feed.known_ids = self.known_ids()  # accounts may have been added since
            elif kind == "off":
                self.link = "off"
                self.last = value + ". Set PacketSendRate (e.g. 10) in TAStatsAPI.ini and restart the game."
            elif kind == "log":
                self.last = f"{datetime.now():%H:%M} {value}"
                self._file_log(value)
            elif kind in ("rate", "error"):  # evidence for odd game modes (statsapi.log in the data folder)
                self._file_log(f"{kind}: {value}")
            elif kind == "game":
                self._save(value)
        live = self.feed.live
        if self.link == "off":
            self._status("error", self.last)
        elif self.link == "waiting":
            self._status("wait", "Waiting for Rocket League's Stats API" + (f". Last: {self.last}" if self.last else ""))
        elif live and live.get("playlist") is not None and not live.get("online") and not self.feed.save_offline:
            self._status("ok", f"Offline game ({playlist_name(live['playlist'])}: freeplay, training or bots), "
                               "not saved")
        elif live and live.get("playlist") is not None:
            sc = live.get("scores") or {}
            mine = live.get("my_team")
            score = (f"{sc.get(0, 0)}-{sc.get(1, 0)} (blue-orange)" if mine is None
                     else f"you {sc.get(mine, 0)}-{sc.get(1 - mine, 0)}")
            clock = "" if live.get("time_left") is None else f", {live['time_left'] // 60}:{live['time_left'] % 60:02d}"
            self._status("live", f"In a match: {playlist_name(live['playlist'])}, {score}"
                                 f"{' OT' if live.get('overtime') else clock}")
        else:
            self._status("ok", f"Connected to the Stats API ({self.link.split('(')[-1].rstrip(')')})"
                               + (f". Last: {self.last}" if self.last else ""))

    @staticmethod
    def _file_log(line: str) -> None:
        try:
            path = DATA_DIR / "statsapi.log"
            if path.exists() and path.stat().st_size > 1_000_000:
                path.replace(path.with_suffix(".log.old"))
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
        except OSError:
            pass

    def _save(self, rec: stats.GameRecord) -> None:
        try:
            key = data.profile_for_player(rec.self_id, rec.self_name)
            gid = data.save_game(key, rec)
        except Exception as exc:  # a locked or broken database must not stop the reader
            self.last = f"{datetime.now():%H:%M} couldn't save a game: {exc}"
            return
        line = (f"{'Win' if rec.won else 'Loss'} {rec.team_goals}-{rec.opp_goals}"
                f"{' OT' if rec.overtime else ''} in {playlist_name(rec.playlist_id)}")
        self.last = f"{datetime.now():%H:%M} saved {line}" if gid else f"{datetime.now():%H:%M} already saved"
        if gid:
            self.saved.emit(key, line)


class Bridge(QObject):
    """The page's link to Python (QWebChannel object "bridge"): state and events out, clicks in."""

    state = Signal(str)   # the whole view state, JSON
    event = Signal(str)   # one-off commands for the page: {"go": page} or {"modal": {...}}

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window

    @Slot(str, str)
    def call(self, action: str, payload: str) -> None:
        try:
            value = json.loads(payload) if payload else None
        except ValueError:
            value = None
        self.window.on_call(action, value)


class WebPage(QWebEnginePage):
    """Opens links in the browser instead of inside the app; page errors go to the log."""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:
        if level != QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
            print(f"page: {message} ({Path(source).name}:{line})", file=sys.stderr)


RANGES = [("7D", 7), ("30D", 30), ("Season", 90), ("All", None)]  # Season: the last 90 days
GRANULARITIES = [("Game", "raw"), ("Day", "daily"), ("Week", "weekly")]
RANK_WINDOW = timedelta(days=120)
INSTANCE_NAME = f"RLUtils-{getpass.getuser()}"
WEB_DIR = Path(__file__).resolve().parent / "web"
EDGES = {"n": Qt.TopEdge, "s": Qt.BottomEdge, "w": Qt.LeftEdge, "e": Qt.RightEdge}


def _ago(when: datetime | None) -> str:
    if when is None:
        return "never"
    mins = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    return "just now" if mins < 1 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago"


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, n // 1024)} KB"


class MainWindow(QMainWindow):
    """A frameless window holding the web UI (rlutils/web): title bar, rail, header, pages and status bar are
    all drawn by the page. Python keeps the state and does the work; the page shows it and sends clicks."""

    PAGES = ["MMR", "Games", "Play", "Settings"]
    profile_renamed = Signal(str, str)  # old key, new key (emitted from any thread; handled on the UI thread)

    def __init__(self, start_hidden: bool = False):
        super().__init__()
        self.profile_renamed.connect(self._on_profile_renamed)
        data.PROFILE_RENAMED.append(self.profile_renamed.emit)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(ui.make_icon())
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1280, 800)
        self.setMinimumSize(1060, 660)
        self.cfg = load_config()
        if data.migrate_config(self.cfg):  # settings older versions left empty, made explicit (once)
            save_config(self.cfg)
        self._setup: dict | None = None  # the setup panel, while open
        self.bg = Background(self)
        self.thresholds = load_thresholds(THRESHOLDS_PATH)
        self.playlist = self.cfg["overlay"]["playlist"]
        self.profile_key: str | None = None
        self.screen_name = "MMR"
        self.range_key = "30D"
        self.gran_key = "Game"
        self.games_pl: int | None = None
        self.games_kind = "All"  # All / Ranked / Casual
        self.selected_game: int | None = None
        self._db_mtime = None
        self._quitting = False
        self._tray_hint_shown = False
        self._busy = False                      # switching account / adding one
        self._phase = "idle"                    # Play: idle, switch, launch, running
        self._launch_until = 0.0
        self._play_note: tuple[str, str, str] | None = None  # (colour, title, detail) shown under Play
        self._accounts: list[dict] = []
        self._accounts_state = ("loading", "")  # loading / ok / error, text
        self._accounts_at: datetime | None = None
        self._rl_running = False
        self._procs_busy = False
        self._msg = ("", 0.0)
        self._vm: dict = {}
        self._last_json = ""
        self._games_all: list[dict] = []

        self.view = QWebEngineView(self)
        self.view.setPage(WebPage(self.view))
        self.view.page().setBackgroundColor(QColor("#202020"))
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        self.bridge = Bridge(self)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.setCentralWidget(self.view)
        self.view.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))
        self._build_tray()

        self.reload_profiles(select=self.cfg["overlay"]["profile"] or None, push=False)
        self._imp: dict | None = None                    # the Import history panel, while open
        self._imp_preview: importer.HistoryPreview | None = None
        self._imp_payload = None                         # the pasted history, until it is imported
        self.vibrance = VibranceWatcher(self)
        self.vibrance.status.connect(lambda _text: self.push())
        self.games = GamesWatcher(self)
        self.games.status.connect(lambda _s, _t: self.push())
        self.games.saved.connect(self._on_game_saved)
        self.live = PsyNetWatcher(self)
        self.live.status.connect(self.push)
        self.live.saved.connect(self._on_live_saved)
        self.signin = EpicSignIn(self, self.live.store)
        self.signin.changed.connect(self.push)
        self.signin.signed_in.connect(self._signed_in)
        self.updates = UpdateChecker(self)
        self.updates.found.connect(self.push)
        self.vibrance.start()
        self.games.start()
        self.live.start()
        self.updates.start()
        self.reload_accounts()
        if not self.cfg["app"].get("setup_done") or not self.live.accounts():
            self.open_setup()  # the Epic sign-in is required; on first start also the game and the Stats API

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(2000)
        self.clock = QTimer(self)  # "2 min ago" texts
        self.clock.timeout.connect(self.push)
        self.clock.start(20_000)
        self._poll()
        for i in range(len(self.PAGES)):  # Ctrl+1..4 also when the page doesn't have focus
            act = QAction(self)
            act.setShortcut(f"Ctrl+{i + 1}")
            act.triggered.connect(lambda _checked=False, n=i: self.go(self.PAGES[n]))
            self.addAction(act)
        self.start_hidden = start_hidden and QSystemTrayIcon.isSystemTrayAvailable()

    # --- the page --------------------------------------------------------------

    def on_call(self, action: str, v) -> None:
        handler = {
            "ready": self._ready, "screen": self._set_screen,
            "move": lambda _v: self.windowHandle().startSystemMove(),
            "resize": self._start_resize, "minimize": lambda _v: self.showMinimized(),
            "maximize": lambda _v: self.showNormal() if self.isMaximized() else self.showMaximized(),
            "close": lambda _v: self.close(), "toTray": lambda _v: self.to_tray(),
            "pickProfile": self._pick_profile, "newProfile": lambda _v: self._new_profile(),
            "trackPlayer": self._track_player, "removeProfile": self._remove_profile,
            "removeProfileConfirmed": self._remove_profile_confirmed,
            "setPlaylist": self._set_playlist, "setRange": self._set_range, "setGran": self._set_gran,
            "setGamesPlaylist": self._set_games_playlist, "setGamesKind": self._set_games_kind, "selectGame": self._select_game,
            "deleteGame": self._delete_game,
            "play": lambda _v: self.play(), "pickAccount": self._pick_account,
            "refreshAccounts": lambda _v: self.reload_accounts(),
            "addAccount": lambda _v: self.add_account(), "addAccountConfirmed": lambda _v: self._add_account(),
            "addCurrent": lambda _v: self.save_current_account(),
            "importOpen": lambda _v: self.open_import(), "importCheck": self._import_check,
            "importConfirm": lambda _v: self._import_confirm(), "importBack": lambda _v: self._import_back(),
            "importCancel": lambda _v: self._import_close(),
            "setupOpen": lambda v: self.open_setup(v if v in self.SETUP_STEPS else "epic"),
            "setupGo": self._setup_go, "setupFinish": self._setup_finish,
            "findGame": self._find_game, "chooseGame": self._choose_game, "setLaunch": self._set_launch,
            "enableStats": self._enable_stats,
            "epicLogin": lambda _v: self.open_epic_login(), "epicLogout": self._epic_logout,
            "epicOpen": lambda _v: self.signin.open_page(), "epicCancel": lambda _v: self.signin.cancel(),
            "liveRefresh": lambda _v: self.live.tick(),
            "updateOpen": self._ask_update, "updateInstall": lambda _v: self.updates.install(),
            "updatePage": lambda _v: self.updates.latest and QDesktopServices.openUrl(QUrl(self.updates.latest["url"])),
            "updateSkip": self._skip_update,
            "setVibrance": self._set_vibrance, "toggleObs": self._toggle_obs,
            "toggle": self._toggle_setting, "setPath": self._set_path, "browse": self._browse,
            "import": lambda _v: self.open_import(),
            "export": lambda _v: self.export_history(),
            "openFolder": lambda _v: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR))),
            "tray": self._tray_action,
        }.get(action)
        if handler is None:
            print(f"page: unknown action {action!r}", file=sys.stderr)
            return
        handler(v)
        self.push()

    def _ready(self, _screen) -> None:
        self._last_json = ""  # the page was (re)loaded: send everything, on the page asked for before it loaded
        self.bridge.event.emit(json.dumps({"go": self.screen_name}))

    def _set_screen(self, name) -> None:
        if name in self.PAGES and name != self.screen_name:
            self.screen_name = name
            if name == "Play":
                self._update_processes()

    def go(self, page: str) -> None:
        self.screen_name = page
        self.bridge.event.emit(json.dumps({"go": page}))

    def modal(self, title: str, text: str, buttons: list[dict] | None = None, field: dict | None = None) -> None:
        """A dialog drawn by the page. buttons: [{label, a (action to call), v, primary, danger}]. field: a text
        box ({placeholder}); its text goes to the button's action as {"value": ...}."""
        self.bridge.event.emit(json.dumps({"modal": {"title": title, "text": text, "field": field,
                                                     "buttons": buttons or [{"label": "OK", "primary": True}]}}))

    def message(self, text: str, seconds: float = 15) -> None:
        self._msg = (text, time.monotonic() + seconds)
        QTimer.singleShot(int(seconds * 1000) + 50, self.push)
        self.push()

    def _start_resize(self, edge) -> None:
        edges = Qt.Edges()
        for ch in str(edge):
            edges |= EDGES.get(ch, Qt.Edges())
        if edges:
            self.windowHandle().startSystemResize(edges)

    def push(self) -> None:
        """Send the view state to the page if anything changed."""
        if not hasattr(self, "games") or "mmr" not in self._vm:
            return  # still starting
        try:
            state = self._state()
        except Exception:  # a bug in one view must not stop the others updating later
            import traceback
            traceback.print_exc()
            return
        text = json.dumps(state, separators=(",", ":"), default=str)
        if text != self._last_json:
            self._last_json = text
            self.bridge.state.emit(text)

    # --- window and tray -------------------------------------------------------

    def _build_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(ui.make_icon(), self)
        self.tray.setToolTip(APP_NAME)
        self.tray_menu = QMenu()
        self.tray_menu.aboutToShow.connect(self._fill_tray_menu)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda reason: self.show_from_tray() if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick)
            else None)
        self.tray.show()

    def _tray_items(self) -> list[dict]:
        account = self._current_account()
        return [
            {"label": f"Open {UI_NAME}", "a": "open", "bold": True},
            {"label": "Play", "a": "play", "right": account["name"] if account else ""},
            {"label": "Import MMR history…", "a": "import"},
            {"label": "Vibrance", "a": "vibrance", "right": f"{int(self.cfg['vibrance']['pct'])}%"},
            {"sep": True},
            {"label": "Start with Windows", "a": "autostart", "check": is_enabled()},
            {"label": "Quit", "a": "quit"},
        ]

    def _fill_tray_menu(self) -> None:
        self.tray_menu.clear()
        for item in self._tray_items():
            if item.get("sep"):
                self.tray_menu.addSeparator()
                continue
            label = item["label"] + (f"\t{item['right']}" if item.get("right") else "")
            act = self.tray_menu.addAction(label)
            if "check" in item:
                act.setCheckable(True)
                act.setChecked(item["check"])
            if item.get("bold"):
                f = act.font()
                f.setBold(True)
                act.setFont(f)
            act.triggered.connect(lambda _c=False, a=item["a"]: self._tray_action(a))

    def _tray_action(self, a) -> None:
        if a == "open":
            self.show_from_tray()
        elif a == "play":
            self.show_from_tray()
            self.go("Play")
            self.play()
        elif a == "import":
            self.show_from_tray()
            self.open_import()
        elif a == "vibrance":
            self.show_from_tray()
            self.go("Play")
        elif a == "autostart":
            self._set_autostart(not is_enabled())
        elif a == "quit":
            self.quit_app()
        self.push()

    def show_from_tray(self) -> None:
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        self.live.timer.stop()
        self.games.stop()
        self.vibrance.restore()
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        self._quitting = True
        super().closeEvent(event)
        self.quit_app()

    def to_tray(self) -> None:
        """The footer's Tray button: hide the window; the app keeps running in the tray."""
        if not self.tray:
            self.showMinimized()
            return
        self.hide()
        self._tray_hint()

    def _tray_hint(self) -> None:
        if not self._tray_hint_shown and self.tray:
            self.tray.showMessage(APP_NAME, "Still running in the tray, tracking your MMR and saving "
                                            "games. Click the icon to open it again.", QSystemTrayIcon.Information, 4000)
            self._tray_hint_shown = True

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self.push()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _allow_taskbar_minimize(self)

    # --- profiles and selections -----------------------------------------------

    def current_profile(self) -> dict | None:
        return data.get_profile(self.profile_key) if self.profile_key else None

    def reload_profiles(self, select: str | None = None, push: bool = True) -> None:
        keys = [p["key"] for p in data.list_profiles()]
        want = select or self.profile_key
        self.profile_key = want if want in keys else (keys[0] if keys else None)
        if self.profile_key and self.cfg["overlay"]["profile"] != self.profile_key:
            self.cfg["overlay"]["profile"] = self.profile_key
            save_config(self.cfg)
        self.selected_game = None
        if self.playlist not in self._playlists():
            self.playlist = TRACKED_PLAYLISTS[0]
        self.refresh(push=push)

    def _playlists(self) -> list[int]:
        ids = list(TRACKED_PLAYLISTS)
        if self.profile_key:
            ids += sorted(set(data.playlists_with_snapshots(self.profile_key)) - set(TRACKED_PLAYLISTS))
        return ids

    def _new_profile(self) -> None:
        self.modal("Track a player", "Enter someone's Epic name to follow their rank. Their MMR history is "
                   "imported from tracker.gg next (you paste it in; nothing updates on its own: live MMR "
                   "needs that player's own Epic sign-in).\n\n"
                   "Your own accounts are added by picking them on the Play page.",
                   [{"label": "Cancel"}, {"label": "Next", "a": "trackPlayer", "primary": True}],
                   field={"placeholder": "Epic name"})

    def _track_player(self, v) -> None:
        name = str((v or {}).get("value") or "").strip()
        if not name:
            return
        key = next((p["key"] for p in data.list_profiles() if p["key"].lower() == name.lower()), None)
        if key is None:
            key = name
            data.upsert_profile(key, None)
            tracked = self.cfg["tracker"].setdefault("tracked", [])
            if key not in tracked:
                tracked.append(key)
            save_config(self.cfg)
        self.reload_profiles(select=key)
        self.go("MMR")
        self.open_import(key)

    def _pick_profile(self, key) -> None:
        if key and key != self.profile_key:
            self.reload_profiles(select=key)

    def _remove_profile(self, key) -> None:
        if key in self.cfg["tracker"].get("tracked", []):
            self.modal("Stop tracking", f"Stop tracking {key} and delete their saved MMR history?",
                       [{"label": "Cancel"}, {"label": "Remove", "a": "removeProfileConfirmed", "v": key, "danger": True}])

    def _remove_profile_confirmed(self, key) -> None:
        tracker = self.cfg["tracker"]
        if key not in tracker.get("tracked", []):
            return
        data.delete_profile(key)
        tracker["tracked"].remove(key)
        tracker["ids"].pop(key, None)
        save_config(self.cfg)
        self.reload_profiles(select=None if key == self.profile_key else self.profile_key)
        self.message(f"Stopped tracking {key}.", 6)

    def _set_playlist(self, pid) -> None:
        if pid is None:
            return
        self.playlist = int(pid)
        if self.cfg["overlay"]["playlist"] != self.playlist:
            self.cfg["overlay"]["playlist"] = self.playlist
            save_config(self.cfg)
        self.refresh(push=False)

    def _set_range(self, key) -> None:
        if key in dict(RANGES):
            self.range_key = key
            self.refresh(push=False)

    def _set_gran(self, key) -> None:
        if key in dict(GRANULARITIES):
            self.gran_key = key
            self.refresh(push=False)

    def _set_games_playlist(self, pid) -> None:
        self.games_pl = None if pid is None else int(pid)
        self.selected_game = None
        self._build_games()

    def _set_games_kind(self, kind) -> None:
        if kind in viewmodel.KINDS and kind != self.games_kind:
            self.games_kind = kind
            self.games_pl = None
            self.selected_game = None
            self._build_games()

    def _select_game(self, gid) -> None:
        if gid is not None and gid != self.selected_game:
            self.selected_game = int(gid)
            self._build_games()

    def _delete_game(self, gid) -> None:
        if gid is None:
            return
        data.delete_game(int(gid))
        if self.selected_game == gid:
            self.selected_game = None
        self.refresh(push=False)
        self.message("Game deleted.", 6)

    def _on_game_saved(self, profile_key: str, line: str) -> None:
        self.message(f"Game saved for {profile_key}: {line}")
        if profile_key != self.profile_key:
            self.reload_profiles(select=profile_key)  # the game was played on another account
        else:
            self.refresh()

    # --- polling ---------------------------------------------------------------

    def _poll(self) -> None:
        self._update_processes()
        try:
            mtime = DB_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if self._db_mtime is not None and mtime != self._db_mtime:
            self.refresh(push=False)
        self._db_mtime = mtime
        self.push()

    def _update_processes(self) -> None:
        """Check whether Rocket League runs, on a worker thread (never block the UI thread: the page
        stops drawing while it waits), then update the Play state."""
        if self._procs_busy:
            return
        self._procs_busy = True

        def work():
            return "rocketleague.exe" in game.process_names()

        def done(res, err):
            self._procs_busy = False
            if not err:
                self._rl_running = res
                self._apply_processes()
                self.push()

        self.bg.run(work, done)

    def _apply_processes(self) -> None:
        if self._rl_running and self._phase in ("idle", "launch"):
            self._phase = "running"
            self._play_note = None
        elif not self._rl_running and self._phase == "running":
            self._phase = "idle"
            QTimer.singleShot(20_000, self.live.tick)  # the game closed: read the MMR its last match left
        elif self._phase == "launch" and time.monotonic() > self._launch_until:
            self._phase = "idle"
            self._play_note = (viewmodel.WARN, "Rocket League didn't start",
                               "Nothing started within 3 minutes. Check Steam and the Rocket League shortcut in Settings.")

    # --- view state ------------------------------------------------------------

    def refresh(self, push: bool = True) -> None:
        """Reload what comes from the database (readings, games) and rebuild the pages."""
        profile = self.current_profile()
        self._games_all = data.list_games(profile["key"]) if profile else []
        snaps = {}
        for pid in {self.playlist, *{g["playlist_id"] for g in self._games_all}}:
            rows = data.list_snapshots(profile["key"], pid) if profile else []
            snaps[pid] = [(datetime.fromisoformat(r["recorded_at"]), r) for r in rows]
        self._snaps = snaps
        self._deltas = viewmodel.game_mmr_deltas(
            self._games_all, {pid: [(t, r["rating"]) for t, r in rows] for pid, rows in snaps.items()})
        self._positions = viewmodel.session_positions(self._games_all, self._gap())
        self._build_mmr()
        self._build_games()
        self._build_profiles()
        self._readings = len(data.all_snapshots(profile["key"])) if profile else 0
        live = [(pid, data.latest_snapshot(profile["key"], pid, data.SOURCE_PSYNET)) for pid in LIVE_PLAYLISTS] \
            if profile else []
        live = [(pid, row) for pid, row in live if row]
        self._live_last = max(live, key=lambda p: datetime.fromisoformat(p[1]["recorded_at"]), default=None)
        if push:
            self.push()

    def _gap(self) -> timedelta:
        return timedelta(minutes=int(self.cfg.get("session_gap_min", 30)))

    def _rank_of(self, row: dict, pid: int) -> tuple[int | None, int | None]:
        return viewmodel.rank_of(row, self.thresholds.get(pid))

    def _distance(self, profile: dict, pid: int):
        rows = self._snaps.get(pid) or [(datetime.fromisoformat(r["recorded_at"]), r)
                                        for r in data.list_snapshots(profile["key"], pid)]
        if not rows:
            return None, None
        last = rows[-1][1]
        tier, div = self._rank_of(last, pid)
        since = datetime.now(timezone.utc) - RANK_WINDOW
        recent = [(r["rating"], r["tier_id"], r["division_id"]) for t, r in rows if t >= since]
        return last, tier_distance(last["rating"], tier, recent, self.thresholds.get(pid))

    def _build_mmr(self) -> None:
        profile, pid = self.current_profile(), self.playlist
        table = self.thresholds.get(pid)
        rows = self._snaps.get(pid, [])
        days = dict(RANGES)[self.range_key]
        start = datetime.now(timezone.utc) - timedelta(days=days) if days else None
        in_range = [(t, r) for t, r in rows if start is None or t >= start]
        before = next((r["rating"] for t, r in reversed(rows) if start is not None and t < start), None)
        games = [g for g in self._games_all if g["playlist_id"] == pid
                 and (start is None or datetime.fromisoformat(g["ended_at"]) >= start)]
        last, dist = self._distance(profile, pid) if profile else (None, None)

        top = {"rank": "", "mmr": "—", "today": "", "todayColor": viewmodel.MUTED,
               "gem": "#b78cff", "rankColor": "#b9a2ff"}
        if not profile:
            top["rank"] = "Pick an account on the Play page"
            gauge = viewmodel.gauge(0, None, None, None)
            gauge["tier"] = "No profile yet"
        elif not last:
            top["rank"] = f"{viewmodel.title(pid)}: no readings yet"
            gauge = viewmodel.gauge(0, None, None, None)
            gauge["tier"] = "No readings yet"
        else:
            tier, div = self._rank_of(last, pid)
            top["rank"] = viewmodel.rank_text(tier, div) or "Rank unknown"
            top["mmr"] = str(last["rating"])
            midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            prior = next((r["rating"] for t, r in reversed(rows) if t < midnight), None)
            change = last["rating"] - prior if prior is not None else 0
            top["today"] = f"{viewmodel.sign(change)} today"
            top["todayColor"] = viewmodel.WIN if change > 0 else viewmodel.LOSS if change < 0 else viewmodel.MUTED
            gauge = viewmodel.gauge(last["rating"], tier, div, dist)

        chart = viewmodel.chart(in_range, table, dict(GRANULARITIES)[self.gran_key])
        n = len(chart["points"])
        if not profile:
            empty = "No profile yet.\nPick an account on the Play page."
        elif not rows:
            empty = (f"No {viewmodel.title(pid)} readings yet.\nThey come from Rocket League's servers (checked at "
                     "start, after the game closes and every 30 min). Past MMR: Play page > Import history.")
        else:
            empty = "No readings in this range.\nPick a longer range above."
        self._vm["mmr"] = {
            "ranges": [{"id": k, "label": k} for k, _ in RANGES], "range": self.range_key,
            "grans": [{"id": k, "label": k} for k, _ in GRANULARITIES], "gran": self.gran_key,
            "gauge": gauge, "cards": viewmodel.mmr_cards(in_range, before, table, games), "chart": chart,
            "count": f"{n} point{'s' if n != 1 else ''} · {self.gran_key.lower()}", "empty": empty,
            "ranks": viewmodel.rank_table(table, last["rating"] if last else None,
                                          (last.get("tier_id"), last.get("division_id")) if last else None),
        }
        self._vm["top"] = top
        self._vm["pl"] = {"options": [{"id": p, "label": viewmodel.short(p), "more": p not in TRACKED_PLAYLISTS}
                                      for p in self._playlists()],
                          "cur": pid, "title": viewmodel.title(pid)}

    def _build_games(self) -> None:
        vm = viewmodel.games_page(self._games_all, self.games_pl, self.selected_game, self._deltas,
                                  self._positions, datetime.now().astimezone(), self.games_kind)
        self.games_pl = vm["pl"]
        self.selected_game = vm["sel"]
        self._vm["games"] = vm

    def _build_profiles(self) -> None:
        rows = []
        for p in data.list_profiles():
            n = sum(c for _pid, c in data.game_playlists(p["key"]))
            readings = len(data.all_snapshots(p["key"]))
            tracked = p["key"] in self.cfg["tracker"].get("tracked", [])
            rows.append({"key": p["key"], "initial": (p["key"][:1] or "?").upper(), "on": p["key"] == self.profile_key,
                         "tracked": tracked,
                         "sub": f"Tracked · {readings:,} readings" if tracked
                         else f"{n:,} game{'s' if n != 1 else ''} · {readings:,} readings"})
        self._vm["profiles"] = rows

    def _state(self) -> dict:
        profile = self.current_profile()
        return {
            "app": {"title": UI_NAME, "version": VERSION, "max": self.isMaximized()},
            "profile": {"name": profile["key"], "initial": profile["key"][:1].upper()} if profile else None,
            "profiles": self._vm["profiles"], "top": self._vm["top"], "pl": self._vm["pl"],
            "mmr": self._vm["mmr"], "games": self._vm["games"],
            "live": self._live_view(), "signin": self.signin.view(), "imp": self._import_view(), "setup": self._setup_view(),
            "api": self._api_view(), "play": self._play_view(),
            "settings": self._settings_view(profile), "status": self._status_view(), "tray": self._tray_items(),
            "update": self._update_view(),
        }

    def _live_view(self) -> dict:
        state, text = self.live.state
        color = {"ok": viewmodel.WIN, "busy": viewmodel.WARN, "paused": viewmodel.WARN,
                 "error": viewmodel.LOSS}.get(state, viewmodel.FAINT)
        names = ", ".join(n or a[:6] for a, n in self.live.accounts())
        title = {"ok": "Live MMR", "busy": "Live MMR · checking", "paused": "Live MMR · paused",
                 "error": "Live MMR · couldn't read it", "off": "Not signed in to Epic"}.get(state, "Live MMR")
        detail = (text + (f" · {names}" if names else "")) if state != "off" else \
            "Sign in to Epic: your MMR for every ranked playlist (1v1, 2v2, 3v3, 4v4 and the extra modes) " \
            "comes straight from Rocket League's servers."
        last = getattr(self, "_live_last", None)
        if last:
            pid, row = last
            when = datetime.fromisoformat(row["recorded_at"]).astimezone()
            day = "" if when.date() == datetime.now().date() else f"{when:%d %b} "
            last_text = f"Last change: {viewmodel.short(pid)} {row['rating']} at {day}{when:%H:%M}"
        else:
            last_text = "No readings yet for this profile"
        return {"on": bool(names), "busy": state == "busy", "color": color, "title": title, "detail": detail,
                "last": last_text,
                "note": "Checked when the app starts, when Rocket League closes and every "
                        f"{int(self.cfg['psynet'].get('every_min', 30))} minutes; never while the game runs.",
                "short": {"ok": "live", "busy": "checking", "paused": "paused", "error": "error"}.get(state, "off")}

    def _epic_rows(self) -> list[dict]:
        return [{"id": a, "name": n or a[:6]} for a, n in self.live.accounts()]

    # --- live MMR: the Epic sign-in (psynet.py) ------------------------------------------------------------

    def _offer_once(self, what: str) -> bool:
        """True the first time sign-in is offered for `what` (a switcher account id); remembered."""
        asked = self.cfg["psynet"].setdefault("asked", [])
        if what.lower() in asked:
            return False
        asked.append(what.lower())
        save_config(self.cfg)
        return True

    def ask_epic_login(self, again: str | None = None) -> None:
        if self.signin.state != "idle":
            return
        if again:
            title = "Sign in to Epic again"
            lead = (f"The saved Epic sign-in for {again} stopped working (the password changed, or it was removed "
                    "on epicgames.com). Sign in once more to keep tracking its MMR.")
        else:
            title = "Track this account's MMR?"
            lead = ("Sign in to Epic with this account too and RLUtils tracks its MMR as well. Your browser opens "
                    "Epic's sign-in page.")
        self.modal(title, f"{lead}\n\n{PRIVACY_TEXT}",
                   [{"label": "Not now"}, {"label": "Sign in with Epic", "a": "epicLogin", "primary": True}])

    def open_epic_login(self) -> None:
        """Epic's sign-in in the user's browser; the page shows the code to approve and waits (EpicSignIn)."""
        self.signin.start()

    def _signed_in(self, account: str, name: str) -> None:
        self.live.expired.discard(account)
        self._offer_once(account)
        key = data.profile_for_epic_account(account, name)
        self.reload_profiles(select=key, push=False)
        if self._setup and self._setup["step"] == "epic":
            if self._setup.get("forced"):
                self._setup = None  # already set up: only the sign-in was missing
            else:
                self._setup.update(step="game", error="")
        self.message(f"Signed in to Epic as {name or account[:6]}: reading your MMR")
        self.live.tick()
        self.push()

    def _epic_logout(self, account) -> None:
        account = str(account or "")
        self.bg.run(lambda: psynet.sign_out(self.live.store, account), lambda _r, _e: self._after_logout())
        self.live.expired.discard(account)

    def _after_logout(self) -> None:
        self.message("Signed out: the saved Epic sign-in was deleted")
        if not self.live.accounts():
            self.open_setup("epic")  # MMR tracking needs a signed-in account
        self.live.tick()
        self.push()
    def _on_profile_renamed(self, old: str, new: str) -> None:
        """A profile saved under a garbled name got its real one (data.profile_for_epic_account): move the
        settings that name it along."""
        cfg = self.cfg
        if cfg["overlay"]["profile"] == old:
            cfg["overlay"]["profile"] = new
        ids = cfg["tracker"].setdefault("ids", {})
        if old in ids:
            ids[new] = ids.pop(old)
        tracked = cfg["tracker"].setdefault("tracked", [])
        cfg["tracker"]["tracked"] = [new if k == old else k for k in tracked]
        save_config(cfg)
        if self.profile_key == old:
            self.profile_key = new
        self.reload_profiles(select=self.profile_key)

    def _on_live_saved(self, profile_key: str, added: int) -> None:
        self.message(f"Live MMR: {added} new reading{'s' if added != 1 else ''} for {profile_key}")
        if self.profile_key is None:
            self.reload_profiles(select=profile_key)
        else:
            self.refresh()

    def _import_view(self) -> dict | None:
        imp = self._imp
        if not imp:
            return None
        key = imp["profile"]
        tid = self._tracker_id(key)
        name = self._tracker_name(key)
        v = {"profile": key, "step": imp["step"], "error": imp.get("error") or "", "info": imp.get("info") or "",
             "clear": imp["clear"], "profileUrl": importer.profile_url(name), "profileName": name,
             "historyUrl": importer.history_url(tid) if tid else "", "trackerId": tid}
        p = self._imp_preview
        if imp["step"] == "preview" and p:
            v["preview"] = {
                "identity": p.identity,
                # tracker.gg's readings are days (00:00 UTC): shown as those days, not moved to local time
                "range": f"{p.first:%d %b %Y} – {p.last:%d %b %Y}",
                "total": p.total, "new": len(p.new), "stored": p.stored, "conflicts": p.conflicts,
                "malformed": p.malformed, "examples": p.examples,
                "playlists": [{"name": r.name, "n": r.readings, "new": r.new, "ranked": r.ranked} for r in p.playlists],
            }
        if imp["step"] == "done":
            v["result"] = imp.get("result", "")
        return v

    def _api_view(self) -> dict:
        state, text = self.games.state
        w = self.games
        green = {"color": viewmodel.WIN, "bg": "rgba(108,203,95,.08)", "border": "rgba(108,203,95,.25)"}
        yellow = {"color": viewmodel.WARN, "bg": "rgba(252,225,0,.08)", "border": "rgba(252,225,0,.25)"}
        red = {"color": viewmodel.LOSS, "bg": "rgba(255,153,164,.08)", "border": "rgba(255,153,164,.25)"}
        if state == "live":
            live = w.feed.live or {}
            sc, mine = live.get("scores") or {}, live.get("my_team")
            score = "" if mine is None else f" · {sc.get(mine, 0)}–{sc.get(1 - mine, 0)}"
            pl = live.get("playlist")  # the feed moves on in its own thread; the state can be a step behind
            v = {"label": "live", "detail": ("In match" + (f" · {viewmodel.title(pl)}" if pl is not None else "")
                                             + score + (" OT" if live.get("overtime") else "")), **green}
        elif state == "ok":
            v = {"label": "connected", "detail": "Offline game, not saved" if text.startswith("Offline")
                 else (w.last.split(" ", 1)[-1] if w.last else "Rocket League running"), **green}
        elif state == "wait":
            v = {"label": "waiting", "detail": "Waiting for the Stats API" if self._rl_running
                 else "Rocket League not running", **yellow}
        elif state == "error":
            v = {"label": "off", "detail": "Enable it in TAStatsAPI.ini", **red}
        else:
            v = {"label": "off", "detail": "Turned off in config.json", **red}
        v["full"] = text
        return v

    def _current_account(self) -> dict | None:
        want = self.cfg["launch"]["account"]
        return next((a for a in self._accounts if a["id"] == want), None)

    def _play_view(self) -> dict:
        account = self._current_account()
        name = account["name"] if account else "the account you pick"
        ph = self._phase
        if ph == "switch":
            status = (viewmodel.WARN, "Switching account…", f"Signing the Epic launcher in as {name}")
        elif ph == "launch":
            status = (viewmodel.WARN, "Launching Rocket League…", f"Starting it through {self._launch_text()}")
        elif ph == "running":
            api = "Stats API connected" if self.games.state[0] in ("ok", "live") else "waiting for the Stats API"
            status = (viewmodel.WIN, "Running", f"Rocket League is running · {api}")
        elif self._play_note:
            status = self._play_note
        elif self._switches(account):
            status = (viewmodel.MUTED, "Ready", f"Switches to {name} (Epic), then starts Rocket League through "
                                                f"{self._launch_text()}")
        else:
            status = (viewmodel.MUTED, "Ready", f"Starts Rocket League through {self._launch_text()}"
                                                + ("" if self._accounts else ", on the account the Epic launcher has"))
        busy = ph in ("switch", "launch")
        btn = {"label": "Running" if ph == "running" else "Starting" if busy else "Play",
               "bg": "#263a24" if ph == "running" else "#4a3560" if busy else viewmodel.ACCENT,
               "color": viewmodel.WIN if ph == "running" else "#e6ccff" if busy else "#1a0b24"}
        a_state, a_text = self._accounts_state
        note = ("Refreshing…" if a_state == "loading" else "Couldn't read accounts" if a_state == "error"
                else f"Refreshed {_ago(self._accounts_at)}")
        accounts = [{"id": a["id"], "name": a["name"], "platform": "EPIC", "on": account is not None and a["id"] == account["id"],
                     "note": "Signed in to the Epic launcher" if a["active"] else "Saved in the account switcher"}
                    for a in self._accounts]
        v = self.cfg["vibrance"]
        vib_state = [s for s in (self.vibrance.screen_state, self.vibrance.obs_state) if s]
        return {"phase": ph, "btn": btn, "status": dict(zip(("color", "title", "detail"), status)),
                "accounts": accounts, "refreshNote": note, "busy": self._busy or busy,
                "noAccounts": a_text if a_state == "error" else ("Reading saved accounts…" if a_state == "loading"
                                                                 else "No accounts saved. Play starts the game on the "
                                                                      "account the Epic launcher is signed into. To "
                                                                      "switch between several, add them here."),
                "vib": int(v["pct"]), "obs": bool(v["obs"]), "vibState": vib_state}

    def _settings_view(self, profile: dict | None) -> dict:
        if profile:
            tid = self._tracker_id(profile["key"])
            first = min((g["ended_at"] for g in self._games_all), default=None)
            n = len(self._games_all)
            rows = [("Display name", profile["key"]),
                    ("tracker.gg ID", tid or "not confirmed yet (Import history)"),
                    ("Epic account", (profile["player_id"] or "none (imported history only)").split("|")[1]
                     if "|" in (profile["player_id"] or "") else (profile["player_id"] or "none (imported history only)")),
                    ("Games tracked", f"{n:,} · since {datetime.fromisoformat(first).astimezone():%b %Y}" if first
                     else "None yet")]
        else:
            rows = [("Display name", "No profile yet")]
        try:
            size = _size(DB_PATH.stat().st_size)
        except OSError:
            size = "not created yet"
        readings = self._readings
        launch = self.cfg["launch"]
        return {
            "profile": rows,
            "profileNote": "The profile follows the account picked on the Play page; a profile is added the first "
                           "time an account is picked or plays. Live MMR, imported history and "
                           "saved games go to it.",
            "toggles": [
                {"key": "start_with_windows", "label": "Start with Windows", "on": is_enabled(),
                 "sub": "Launch minimized when you sign in"},
                {"key": "check_updates", "label": "Check for updates",
                 "on": bool(self.cfg["app"].get("check_updates", True)),
                 "sub": "Once a day, ask GitHub whether a newer RLUtils is out (it installs only when you click Update)"},
            ],
            "paths": [
                {"key": "switcher", "label": "Account switcher", "value": launch["switcher"],
                 "placeholder": "Built in (accounts saved in the data folder). Or pick your own EpicSwitcher.ps1"},
            ],
            "rl": self._rl_view(),
            "data": f"{DB_PATH.name} · {size} · {readings:,} readings · {len(self._games_all):,} games",
            "dataDir": str(DATA_DIR),
            "epic": self._epic_rows(), "privacy": PRIVACY_TEXT,
        }

    def _installs(self) -> list[dict]:
        """Rocket League installs (game.find_installs), looked up once and again on Find automatically."""
        if getattr(self, "_installs_cache", None) is None:
            try:
                self._installs_cache = game.find_installs()
            except OSError:
                self._installs_cache = []
        return self._installs_cache

    def _stats_files(self) -> tuple[Path, Path | None]:
        """(the Documents copy, the install folder's file) of the Stats API setting. The install is the one Play
        uses when it's known, else the first found."""
        installs = self._installs()
        method = self.cfg["launch"].get("method")
        install = next((i for i in installs if i["method"] == method), installs[0] if installs else None)
        return stats.stats_ini_path(), (stats.install_ini_path(Path(install["dir"])) if install else None)

    def _stats_api_state(self) -> dict:
        return stats.stats_api_state(*self._stats_files())

    def _rl_view(self) -> dict:
        """Settings > Rocket League: how Play starts the game, and whether the Stats API is on."""
        api = self._stats_api_state()
        if api["enabled"]:
            api_text, api_color = f"On ({api['rate']} updates a second, port {api['port']})", viewmodel.WIN
        elif api["exists"]:
            api_text, api_color = "Off: the Games page, playstyle, teammates and per-game MMR changes need it", viewmodel.WARN
        else:
            api_text, api_color = ("Its settings file doesn't exist yet (start Rocket League once), or enable it "
                                   "here to create it"), viewmodel.WARN
        return {"launch": f"Play starts it through {self._launch_text()}", "method": self.cfg["launch"].get("method"),
                "api": api_text, "apiColor": api_color, "apiOn": api["enabled"], "apiPath": api["path"],
                "gameRunning": self._rl_running}

    def _status_view(self) -> dict:
        n, length = viewmodel.current_session(self._games_all, self._gap(), datetime.now(timezone.utc))
        mins = int(length.total_seconds() // 60)
        session = (f"Session · {n} game{'s' if n != 1 else ''} · "
                   + (f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m")) if n else "No session going"
        msg, until = self._msg
        return {"session": session,
                "msg": msg if time.monotonic() < until else ""}

    # --- play ------------------------------------------------------------------

    def reload_accounts(self, keep_status: bool = False) -> None:
        self._accounts_state = ("loading", "")
        if not keep_status:
            self._play_note = None

        def done(rows, err):
            if err:
                self._accounts_state = ("error", str(err))
                self.push()
                return
            self._accounts = rows
            self._accounts_state = ("ok", "")
            signed = {a for a, _n in self.live.accounts()}
            new = [r for r in rows if r["id"] and r["id"].lower() not in signed and self.cfg["app"].get("setup_done")
                   and self._offer_once(r["id"])]
            if new:  # an account added to the switcher: offer live MMR for it, once
                QTimer.singleShot(500, self.ask_epic_login)
            self._accounts_at = datetime.now(timezone.utc)
            if not self._current_account():
                active = next((r["id"] for r in rows if r["active"]), "")
                if active:
                    self._pick_account(active)
            else:
                self._follow_account(self._current_account())
            self.push()

        script, store = switcher_path(self.cfg), switcher_store(self.cfg)
        self.bg.run(lambda: game.switcher_accounts(script, store), done)

    def _pick_account(self, account_id) -> None:
        row = next((a for a in self._accounts if a["id"] == account_id), None)
        if not row or self._busy:
            return
        if self.cfg["launch"]["account"] != row["id"]:
            self.cfg["launch"]["account"] = row["id"]
            save_config(self.cfg)
        self._play_note = None
        self._follow_account(row)

    def _follow_account(self, row: dict | None) -> None:
        """Show the profile of the account picked on the Play page (added the first time)."""
        if not row or not row.get("id"):
            return
        key = data.profile_for_epic_account(row["id"], row.get("name") or "")
        if key != self.profile_key:
            self.reload_profiles(select=key)

    def add_account(self) -> None:
        if self._busy:
            return
        if game.process_running("RocketLeague.exe"):
            self.modal("Add account", "Close Rocket League first: adding an account restarts the Epic launcher.")
            return
        self.modal("Add account",
                   "The Epic launcher will restart on its sign-in screen.\n\n"
                   "1. Sign in with the account to add (password and 2FA, with \"Remember me\" ticked).\n"
                   "2. Come back here and click \"Add current account\".\n\n"
                   "Your other saved accounts stay saved.\n\n"
                   "How it works: RLUtils saves the launcher's \"Remember me\" sign-in for each account, encrypted "
                   "with Windows so only your Windows user on this PC can read it, and swaps it in when you switch. "
                   "It never sees your password. Don't click \"Sign out\" in the launcher: that ends the saved "
                   "sign-in and you'd have to add the account again.",
                   [{"label": "Cancel"}, {"label": "Restart launcher", "a": "addAccountConfirmed", "primary": True}])

    def _add_account(self) -> None:
        script = switcher_path(self.cfg)
        self._busy = True
        self._play_note = (viewmodel.WARN, "Adding an account…", "Restarting the Epic launcher on its sign-in screen")

        def done(message, err):
            self._busy = False
            self._play_note = ((viewmodel.LOSS, "Couldn't restart the launcher", str(err)) if err else
                               (viewmodel.MUTED, "Sign in, then click Add current account", message))
            self.push()

        store = switcher_store(self.cfg)
        self.bg.run(lambda: game.add_new_account(script, store), done)

    def save_current_account(self) -> None:
        if self._busy:
            return
        script = switcher_path(self.cfg)
        self._busy = True
        self._play_note = (viewmodel.WARN, "Saving the account…", "Saving the account the Epic launcher is signed into")

        def done(message, err):
            self._busy = False
            if err:
                self._play_note = (viewmodel.LOSS, "Couldn't save the account", str(err))
                self.push()
                return
            self._play_note = (viewmodel.WIN, "Account saved", message)
            self.cfg["launch"]["account"] = ""  # select the account just saved (it's now the active one)
            self.reload_accounts(keep_status=True)
            self.push()

        store = switcher_store(self.cfg)
        self.bg.run(lambda: game.save_current_account(script, store), done)

    def _launch_text(self) -> str:
        """How Play starts the game, in words."""
        launch = self.cfg["launch"]
        method, shortcut = launch.get("method", "auto"), rl_shortcut_path(self.cfg)
        if method == "path":
            return f"your {'shortcut' if shortcut and shortcut.suffix.lower() != '.exe' else 'program'} " \
                   f"{shortcut.name if shortcut else '(none chosen)'}"
        return {"epic": "the Epic Games launcher", "steam": "Steam"}.get(method, "Epic or Steam, whichever has it")

    def _switches(self, row: dict | None) -> bool:
        """Whether Play switches the Epic launcher's account first: an account is picked, and the game doesn't
        start through Steam (which doesn't use the Epic launcher's sign-in)."""
        return row is not None and self.cfg["launch"].get("method") != "steam"

    def play(self) -> None:
        """Switch the Epic launcher to the picked account (when accounts are saved), then start the game. With no
        saved accounts it just starts the game, signed into whatever account the launcher has."""
        if self._busy or self._phase != "idle":
            return
        if self._rl_running or game.process_running("RocketLeague.exe"):
            self.modal("Play", "Rocket League is already running. Close it first to play on another account.")
            return
        cfg = self.cfg
        row = self._current_account() if self._accounts_state[0] == "ok" else None
        switch = self._switches(row)
        method, shortcut = cfg["launch"].get("method", "auto"), rl_shortcut_path(cfg)
        script, store = switcher_path(cfg), switcher_store(cfg)
        timeout = int(cfg["launch"]["sign_in_timeout_s"])
        self._busy = True
        self._phase = "switch" if switch else "launch"
        self._launch_until = time.monotonic() + 180
        self._play_note = None

        def work():
            how, target = game.resolve_launch(method, shortcut)  # before touching the launcher: fail early
            message = game.switch_account(script, row["id"], timeout, store) if switch else ""
            game.start_game(how, target)
            return message or f"Starting Rocket League through {self._launch_text()}"

        def done(message, err):
            self._busy = False
            if err:
                self._phase = "idle"
                self._play_note = (viewmodel.LOSS, "Couldn't start", str(err))
            else:
                self._phase = "launch"
                self._launch_until = time.monotonic() + 180
                self.message(message)
                QTimer.singleShot(1500, lambda: self.reload_accounts(keep_status=True))
            self.push()

        self.bg.run(work, done)

    # --- vibrance and settings -------------------------------------------------

    def _set_vibrance(self, pct) -> None:
        try:
            pct = max(50, min(100, int(pct)))
        except (TypeError, ValueError):
            return
        self.cfg["vibrance"]["pct"] = pct
        save_config(self.cfg)
        self.vibrance.obs_retry_at = 0.0  # push the new value to OBS straight away
        self.vibrance.tick()

    def _toggle_obs(self, _v=None) -> None:
        self.cfg["vibrance"]["obs"] = not self.cfg["vibrance"]["obs"]
        save_config(self.cfg)
        self.vibrance.obs_retry_at = 0.0
        self.vibrance.tick()

    def _set_autostart(self, on: bool) -> None:
        try:
            set_enabled(on)
        except OSError as exc:
            self.modal("Start with Windows", f"Couldn't change the setting:\n{exc}")
        self.cfg["app"]["start_with_windows"] = is_enabled()
        save_config(self.cfg)

    def _toggle_setting(self, key) -> None:
        if key == "start_with_windows":
            self._set_autostart(not is_enabled())
            return
        if key == "check_updates":
            on = not self.cfg["app"].get("check_updates", True)
            self.cfg["app"]["check_updates"] = on
            save_config(self.cfg)
            if on:
                self.updates.check()
            else:
                self.updates.latest = None

    def _skip_update(self, _v=None) -> None:
        if self.updates.latest:
            self.cfg["app"]["skip_version"] = self.updates.latest["version"]
            save_config(self.cfg)

    def _update_view(self) -> dict | None:
        u = self.updates.latest
        state, text = self.updates.state
        if not u or (u["version"] == self.cfg["app"].get("skip_version") and state == "idle"):
            return None
        label = {"downloading": f"Downloading RLUtils {u['version']}… {text}%",
                 "installing": f"Installing RLUtils {u['version']}…", "error": text}.get(
            state, f"RLUtils {u['version']} is out · " + ("Update" if self._can_self_update() else "Download"))
        return {"version": u["version"], "label": label, "busy": state in ("downloading", "installing"),
                "error": state == "error"}

    def _can_self_update(self) -> bool:
        u = self.updates.latest
        return bool(u and u.get("asset") and installed_copy())

    def _ask_update(self, _v=None) -> None:
        u = self.updates.latest
        if not u or self.updates.state[0] in ("downloading", "installing"):
            return
        if not self._can_self_update():
            QDesktopServices.openUrl(QUrl(u["url"]))
            return
        mb = round(u["asset"]["size"] / 1e6)
        warn = ("\n\nRocket League is running: the update won't touch it, but RLUtils stops tracking until it "
                "restarts (a few seconds)." if self._rl_running else "")
        self.modal(f"Update to RLUtils {u['version']}?",
                   f"RLUtils downloads the installer from its GitHub release ({mb} MB), checks it against the "
                   "checksum GitHub published for it, installs it and starts again. Your data and sign-in stay."
                   + warn,
                   [{"label": "Not now"}, {"label": "What's new", "a": "updatePage"},
                    {"label": "Update", "a": "updateInstall", "primary": True}])

    def _set_path(self, v) -> None:
        if not isinstance(v, dict):
            return
        key, value = v.get("key"), str(v.get("value") or "").strip().strip('"')
        if key != "switcher":
            return
        self.cfg["launch"]["switcher"] = value
        save_config(self.cfg)
        self.reload_accounts()

    def _browse(self, key) -> None:
        if key != "switcher":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Epic account switcher", str(switcher_path(self.cfg)),
                                              "PowerShell script (*.ps1)")
        if path:
            self._set_path({"key": key, "value": str(Path(path))})

    # --- Rocket League: how Play starts it, and the Stats API (Settings and setup) -------------------------

    def find_game(self) -> list[dict]:
        """Look for Rocket League (Epic, Steam). Picks the first one found unless the user chose a file."""
        installs = self._installs_cache = game.find_installs()
        launch = self.cfg["launch"]
        if installs and launch.get("method") in ("auto", "epic", "steam") and \
                launch.get("method") not in [i["method"] for i in installs]:
            launch["method"] = installs[0]["method"]
            save_config(self.cfg)
        return installs

    def _find_game(self, _v=None) -> None:
        installs = self.find_game()
        if self._setup:
            self._setup.update(found=installs, error="" if installs else
                               "Rocket League wasn't found in Epic Games or Steam. Choose the game or a shortcut yourself.")
            return
        if not installs:
            self.modal("Find Rocket League", "Rocket League wasn't found in Epic Games or Steam.\n\n"
                                             "Choose the game or a shortcut yourself instead.")
            return
        where = "; ".join(f"{'Epic Games' if i['method'] == 'epic' else 'Steam'}: {i['dir']}" for i in installs)
        self.message(f"Found Rocket League ({where}). Play starts it through {self._launch_text()}.")

    def _set_launch(self, method) -> None:
        if method in data.LAUNCH_METHODS and method != "path":
            self.cfg["launch"]["method"] = method
            save_config(self.cfg)

    def _choose_game(self, _v=None) -> None:
        """A file dialog for the game's exe or a shortcut to it (.lnk, .url)."""
        start = rl_shortcut_path(self.cfg)
        path, _ = QFileDialog.getOpenFileName(self, "Rocket League, or a shortcut to it",
                                              str(start.parent if start else Path.home() / "Desktop"),
                                              "Program or shortcut (*.exe *.lnk *.url)")
        if not path:
            return
        problem = game.check_game_file(Path(path))
        if problem:
            if self._setup:
                self._setup["error"] = problem
            else:
                self.modal("Rocket League", problem)
            return
        self.cfg["launch"].update(rl_shortcut=str(Path(path)), method="path")
        save_config(self.cfg)
        if self._setup:
            self._setup["error"] = ""

    def _enable_stats(self, _v=None) -> None:
        """Switch the Stats API on in TAStatsAPI.ini (the user clicked Enable). Not while the game runs: it reads
        the file when it starts and may write it when it closes."""
        if self._rl_running or game.process_running("RocketLeague.exe"):
            text = "Close Rocket League first: it reads this setting when it starts."
            if self._setup:
                self._setup.update(statsMsg=text, statsOk=False)
            else:
                self.modal("Stats API", text)
            return
        user_ini, install_ini = self._stats_files()
        changed, problems = stats.enable_stats_everywhere(user_ini, install_ini)
        ok = stats.read_stats_api(user_ini)["enabled"]
        if ok and not problems:
            text = "Stats API enabled. It starts working the next time you start Rocket League."
        elif ok:
            text = ("Stats API enabled for now, but the game folder's copy couldn't be changed, so a game update may "
                    "switch it off again: " + "; ".join(problems))
        else:
            text = "Couldn't switch it on: " + "; ".join(problems)
        if self._setup:
            self._setup.update(statsMsg=text, statsOk=ok)
        elif ok:
            self.message(text)
        else:
            self.modal("Stats API", text)

    # --- setup: the Epic sign-in (required), the game, the Stats API -----------------------------------------

    SETUP_STEPS = ("epic", "game", "stats", "done")

    def open_setup(self, step: str = "epic") -> None:
        if not self.live.accounts():
            step = "epic"  # nothing else works before the sign-in
        self._setup = {"step": step, "error": "", "found": None, "statsMsg": "", "statsOk": False,
                       "seq": (self._setup or {}).get("seq", 0) + 1,
                       "forced": bool(self.cfg["app"].get("setup_done")) and not self.live.accounts()}

    def _setup_go(self, step) -> None:
        if not self._setup or step not in self.SETUP_STEPS:
            return
        if step != "epic" and not self.live.accounts():
            self._setup.update(step="epic", error="Sign in to Epic first: RLUtils tracks your MMR through it.")
            return
        self._setup.update(step=step, error="")

    def _setup_finish(self, _v=None) -> None:
        if not self.live.accounts():
            self._setup_go("game")  # shows why it can't close yet
            return
        self._setup = None
        if not self.cfg["app"].get("setup_done"):
            self.cfg["app"]["setup_done"] = True
            save_config(self.cfg)

    def _setup_view(self) -> dict | None:
        s = self._setup
        if not s:
            return None
        launch = self.cfg["launch"]
        api = self._stats_api_state()
        found = s["found"]
        return {
            "step": s["step"], "steps": list(self.SETUP_STEPS[:-1]), "seq": s["seq"],
            "error": s["error"], "required": not self.cfg["app"].get("setup_done"),
            "launch": self._launch_text(), "method": launch.get("method"),
            "shortcut": launch.get("rl_shortcut") or "",
            "found": None if found is None else [
                {"method": i["method"], "label": "Epic Games" if i["method"] == "epic" else "Steam",
                 "dir": i["dir"], "on": launch.get("method") == i["method"]} for i in found],
            "apiOn": api["enabled"], "apiExists": api["exists"], "apiPath": str(stats.stats_ini_path()),
            "apiInstallPath": api.get("install_path", ""),
            "statsMsg": s["statsMsg"], "statsOk": s["statsOk"], "gameRunning": self._rl_running,
            "profile": (self.current_profile() or {}).get("key", ""),
            "epic": self._epic_rows(), "privacy": PRIVACY_TEXT,
        }

    # --- data ------------------------------------------------------------------

    # --- import history: a one-time paste of tracker.gg's MMR history (rlutils/importer.py) -------------

    def _tracker_id(self, key: str) -> str:
        """tracker.gg's player id for a profile: confirmed by a pasted tracker.gg profile (tracker.ids), or set
        by hand in config.json (player_history_id, for your own profiles only)."""
        cfg = self.cfg["tracker"]
        override = "" if key in cfg.get("tracked", []) else str(cfg.get("player_history_id") or "").strip()
        return override or str(cfg["ids"].get(key, "") or "")

    def _tracker_name(self, key: str) -> str:
        """The Epic name to look the profile up by on tracker.gg: its account's name in the account switcher,
        else the profile's name."""
        profile = data.get_profile(key)
        account = data.account_hex(profile["player_id"]) if profile else None
        row = next((a for a in self._accounts if account and a["id"].lower() == account), None)
        return (row or {}).get("name") or key

    def open_import(self, key: str | None = None) -> None:
        """Open the Import history panel for `key`, by default the profile of the account picked on the Play page."""
        if key is None:
            account = self._current_account()
            if not account:
                self.modal("Import history", "Pick an account on the Play page first: the history goes to its profile.")
                return
            key = data.profile_for_epic_account(account["id"], account.get("name") or "")
        clear = (self._imp or {}).get("clear", 0) + 1
        self._imp = {"profile": key, "step": "paste", "error": "", "info": "", "clear": clear}
        self._imp_preview = self._imp_payload = None

    def _import_check(self, v) -> None:
        """The user pressed Import: check what they pasted. A tracker.gg profile confirms the player (and gives
        the history link); a history gets a preview to confirm. Nothing is saved here."""
        imp = self._imp
        if not imp or imp["step"] != "paste":
            return
        key = imp["profile"]
        profile = data.get_profile(key) or {"player_id": None}
        try:
            payload = importer.parse_paste(str((v or {}).get("text") or ""))
            kind = importer.kind(payload)
            if kind == "error":
                raise importer.PasteProblem(importer.error_text(payload))
            if kind == "profile":
                found = importer.check_profile(payload, key, profile["player_id"])
                self.cfg["tracker"]["ids"][key] = found.tracker_id
                save_config(self.cfg)
                imp.update(error="", clear=imp["clear"] + 1,
                           info=f"Confirmed: tracker.gg profile {found.handle} (id {found.tracker_id}). Now step 2: "
                                "open the MMR history link and paste that page.")
                return
            if kind != "history":
                raise importer.PasteProblem("That JSON isn't a tracker.gg MMR history or profile. Open one of the "
                                            "links above and copy that page.")
            self._imp_payload = payload
            self._imp_preview = self._preview(key, payload)
            imp.update(step="preview", error="", info="")
        except importer.PasteProblem as exc:
            imp.update(error=str(exc), info="")

    def _preview(self, key: str, payload) -> importer.HistoryPreview:
        tid = self._tracker_id(key)
        hint = (f"The history link above is for {key}'s confirmed tracker.gg profile (id {tid}); the history itself "
                "names no player, so that's as far as it can be checked." if tid else "")
        return importer.preview_history(payload, key, data.all_snapshots(key), hint)

    def _import_confirm(self) -> None:
        """Save the previewed readings: only new ones; a stored reading is never replaced."""
        imp = self._imp
        if not imp or imp["step"] != "preview" or self._imp_payload is None:
            return
        key = imp["profile"]
        try:  # again, against what's stored now (the log may have added readings meanwhile)
            p = self._preview(key, self._imp_payload)
        except importer.PasteProblem as exc:
            imp.update(step="paste", error=str(exc))
            return
        added = data.insert_snapshots(key, p.new)
        parts = [f"{p.stored} were already stored"] if p.stored else []
        if p.conflicts:
            parts.append(f"{p.conflicts} differ from readings already stored for the same day and were left as stored")
        if p.malformed:
            parts.append(f"{p.malformed} couldn't be read")
        imp.update(step="done", clear=imp["clear"] + 1,
                   result=f"Imported {added} reading{'s' if added != 1 else ''} into {key}."
                          + (f" Of the rest, {'; '.join(parts)}." if parts else ""))
        self._imp_preview = self._imp_payload = None
        self.reload_profiles(select=key, push=False)

    def _import_back(self) -> None:
        if self._imp and self._imp["step"] == "preview":
            self._imp.update(step="paste", error="")
            self._imp_preview = None

    def _import_close(self) -> None:
        self._imp = self._imp_preview = self._imp_payload = None

    def export_history(self) -> None:
        profile = self.current_profile()
        if not profile:
            self.modal("Export", "No profile yet.")
            return
        rows = [
            {
                "profile": profile["key"], "playlist_id": r["playlist_id"], "playlist": playlist_name(r["playlist_id"]),
                "recorded_at": r["recorded_at"], "rating": r["rating"],
                "rank": rank_label(*self._rank_of(r, r["playlist_id"])),
                "tier_id": r["tier_id"], "division_id": r["division_id"], "source": r["source"],
            }
            for r in data.all_snapshots(profile["key"])
        ]
        path, selected = QFileDialog.getSaveFileName(self, "Export MMR history", f"{profile['key']}_mmr.csv",
                                                     "CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        target = Path(path)
        as_json = target.suffix.lower() == ".json" or (not target.suffix and selected.startswith("JSON"))
        target = target.with_suffix(".json" if as_json else ".csv")
        try:
            if as_json:
                target.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            else:
                with target.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["profile"])
                    writer.writeheader()
                    writer.writerows(rows)
        except OSError as exc:
            self.modal("Export MMR history", f"Couldn't write the file:\n{exc}")
            return
        self.message(f"Exported {len(rows)} rows to {target}")


def _allow_taskbar_minimize(window) -> None:
    """A frameless window has no minimize box, so clicking its taskbar button does nothing; add the styles
    (they draw nothing without a caption)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        hwnd = ctypes.c_void_p(int(window.winId()))
        style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
        user32.SetWindowLongW(hwnd, -16, style | 0x00020000 | 0x00010000 | 0x00080000)  # MINIMIZEBOX MAXIMIZEBOX SYSMENU
    except (AttributeError, OSError):
        pass


def main() -> int:
    if sys.platform == "win32" and not FROZEN:
        # From source: own taskbar button instead of Python's. The exe sets no ID, so Windows goes by the exe
        # path, the same as a pinned shortcut to RLUtils.exe; then the pinned button is the one that lights up.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RLUtils.App")
        except (AttributeError, OSError):
            pass
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv)

    # One app at a time: a second launch asks the running one to show itself (or, with --quit, to quit: the
    # installer uses it so vibrance is put back before the files are replaced).
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE_NAME)
    if probe.waitForConnected(300):
        probe.write(b"quit" if "--quit" in sys.argv else b"show")
        probe.waitForBytesWritten(300)
        return 0
    if "--quit" in sys.argv:
        return 0
    QLocalServer.removeServer(INSTANCE_NAME)
    server = QLocalServer()
    server.listen(INSTANCE_NAME)

    app.setStyle("Fusion")
    app.setFont(ui.app_font())
    app.setStyleSheet(ui.QSS)  # dialogs and the tray menu
    app.setWindowIcon(ui.make_icon())
    app.setQuitOnLastWindowClosed(False)  # the tray keeps it alive; quit_app/closeEvent end it
    window = MainWindow(start_hidden="--tray" in sys.argv)
    app.aboutToQuit.connect(window.vibrance.restore)  # never leave the desktop at Rocket League's vibrance
    def on_connection():
        sock = server.nextPendingConnection()
        sock.waitForReadyRead(300)
        if bytes(sock.readAll()).startswith(b"quit"):
            window.quit_app()
        else:
            window.show_from_tray()

    server.newConnection.connect(on_connection)
    if not window.start_hidden:
        window.show()
    return app.exec()
