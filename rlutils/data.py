"""Settings, the database, MMR history, ranks, sessions, streaks and match insights.

No Qt widgets here: this is the part the app and the overlay share.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__


# ====================================================================================================
# Config: Paths, endpoints and settings. Everything user-specific lives under data/.
# ====================================================================================================


APP_NAME = "RLUtils"
VERSION = __version__
ROOT = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)


def _data_dir() -> Path:
    # RLUTILS_DATA (or the older RL_TRACKER_DATA) overrides. The installed app reads data_dir.txt next to its
    # exe (install.ps1 points it at the project's data folder, so the exe and a source run share one folder);
    # without that file it uses %LOCALAPPDATA%\RLUtils (or %LOCALAPPDATA%\RLTracker, the name before 0.11, if
    # that one already exists). A source checkout keeps it in ./data next to the code.
    override = os.environ.get("RLUTILS_DATA") or os.environ.get("RL_TRACKER_DATA")
    if override:
        return Path(override)
    if FROZEN:
        try:
            pointed = Path(sys.executable).with_name("data_dir.txt").read_text(encoding="utf-8-sig").strip()
        except OSError:
            pointed = ""
        if pointed:
            return Path(pointed)
        local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        old, new = local / "RLTracker", local / "RLUtils"
        return old if old.is_dir() and not new.exists() else new
    return ROOT / "data"


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "tracker.db"
CONFIG_PATH = DATA_DIR / "config.json"
STATUS_PATH = DATA_DIR / "status.json"  # written by the overlay, read by the app
THRESHOLDS_PATH = DATA_DIR / "rank_thresholds.json"  # rank tables the user imported

# All settings live here so they can be changed in one place.
DEFAULTS = {
    # The profile and playlist the app shows.
    "overlay": {
        "profile": "",          # profile shown; empty = the first one
        "playlist": 11,         # playlist shown: 10 = 1v1, 11 = 2v2, 13 = 3v3
    },
    # Past MMR: a one-time import of tracker.gg's MMR history, pasted by the user (Import history). The
    # tracker.gg player id for a profile is kept in `ids` once a pasted tracker.gg profile confirmed it;
    # player_history_id overrides it for every profile.
    "tracker": {
        "player_history_id": "",
        "ids": {},              # profile key -> tracker.gg player id
        "tracked": [],          # other players followed by Epic name (profile menu > + Track a player)
    },
    # New MMR: read from Rocket League's own log each time you queue ranked (rlutils/rllog.py). `dir` = ""
    # is Documents\My Games\Rocket League\TAGame\Logs.
    "rl_log": {
        "enabled": True,
        "dir": "",
        "every_s": 5,
    },
    # Live MMR from Rocket League's servers (rlutils/psynet.py) for the Epic accounts signed in (epic_login.dat
    # in the data folder, encrypted for this Windows user). `asked`: accounts / "start" already offered sign-in.
    "psynet": {
        "every_min": 30,
        "asked": [],
    },
    # Play: switch the Epic launcher to the picked account (when accounts are saved), then start Rocket League.
    # `method`: "auto" (Epic if installed there, else Steam; found at each start), "epic", "steam", or "path"
    # (`rl_shortcut`: an exe, .lnk or .url the user picked). `switcher`: "" = the switcher built into the app
    # (accounts saved in the data folder's epic-accounts), or another EpicSwitcher.ps1 with its own store.
    "launch": {
        "method": "auto",
        "rl_shortcut": "",
        "switcher": "",
        "account": "",          # Epic account id last picked on the Play page
        "sign_in_timeout_s": 90,
        "migrated": False,      # migrate_config ran
    },
    # Desktop app behaviour.
    # Vibrance while Rocket League is in front: NVIDIA Digital Vibrance (50 = the driver default, 100 = max),
    # restored when another window is in front. With `obs`, the same boost goes on the Rocket League capture
    # in OBS as a Color Correction filter, so recordings show it; `obs_strength` is the filter's saturation at 100%.
    "vibrance": {
        "pct": 50,
        "obs": True,
        "obs_strength": 1.0,
        "obs_url": "ws://127.0.0.1:4455",
        "obs_password": "",
    },
    # Per-game stats from Rocket League's own Stats API (enable it with PacketSendRate > 0 in TAStatsAPI.ini):
    # the WebSocket on `web_port`, else TCP on `port`. `save_offline` also saves matches without a MatchGuid
    # (bots, offline exhibitions).
    "games": {
        "enabled": True,
        "host": "127.0.0.1",
        "web_port": 49124,
        "port": 49123,
        "save_offline": False,
    },
    "app": {
        "minimize_to_tray": False,     # True: minimizing hides the window to the tray
        "close_to_tray": False,        # True: closing the window keeps the app (and the MMR log reader) in the tray
        "start_with_windows": False,    # mirrored to HKCU\...\Run when changed in Settings
        "setup_done": False,            # the first-run setup was finished (Settings > Run setup again reopens it)
        "check_updates": True,          # ask GitHub for the newest release at start and once a day
        "skip_version": "",             # a release the user dismissed: not shown again
    },
    # Matches more than this many minutes apart count as separate sessions (older data only).
    "session_gap_min": 30,
}

OBSOLETE_KEYS = ("phone", "ballchasing", "mmr_api_base", "mmr_poll", "screen_read", "statsapi_host",
                 "statsapi_port", "update_feed_url", "http_timeout_s", "shift")
# Settings of removed features, dropped from a saved config.json when it loads.
OBSOLETE_SUBKEYS = {
    "overlay": ("show_session", "show_streak", "show_mmr", "show_status", "show_rank",
                "tilt_enabled", "tilt_loss_streak", "tilt_mmr_drop", "position", "opacity", "margin",
                "always_show", "font_size", "hotkey"),
    "app": ("auto_overlay",),
    "launch": ("with_shift",),
    "vibrance": ("mode", "lens_strength", "lens_hz"),  # the window lens (removed: too much display lag)
    "tracker": ("profile", "every_min", "hidden_wait_s", "captcha_wait_s"),
}

BUNDLED_SWITCHER = Path(__file__).resolve().parent / "assets" / "EpicSwitcher.ps1"
SWITCHER_STORE = DATA_DIR / "epic-accounts"  # the built-in switcher's saved logins (DPAPI-encrypted, this user only)
LAUNCH_METHODS = ("auto", "epic", "steam", "path")


def switcher_path(cfg: dict) -> Path:
    return Path(cfg["launch"]["switcher"]) if str(cfg["launch"].get("switcher") or "").strip() else BUNDLED_SWITCHER


def switcher_store(cfg: dict) -> Path | None:
    """The folder the built-in switcher keeps accounts in; None for a switcher of the user's own (it has its own)."""
    return None if str(cfg["launch"].get("switcher") or "").strip() else SWITCHER_STORE


def rl_shortcut_path(cfg: dict) -> Path | None:
    value = str(cfg["launch"].get("rl_shortcut") or "").strip().strip('"')
    return Path(value) if value else None


def migrate_config(cfg: dict) -> bool:
    """Fix launch settings from older versions: an unknown `method` becomes "auto", and (once, launch.migrated)
    a shortcut set before methods existed makes the method "path". Returns whether anything changed."""
    launch = cfg["launch"]
    changed = launch.get("method") not in LAUNCH_METHODS
    if changed:
        launch["method"] = "auto"
    if launch.get("migrated"):
        return changed
    launch["migrated"] = True
    if str(launch.get("rl_shortcut") or "").strip():
        launch["method"] = "path"
    return True


PLAYLISTS = {
    0: "Casual",  # Tracker's ID for casual; the game's Stats API reports 1-4 below
    1: "Casual Duel 1v1",
    2: "Casual Doubles 2v2",
    3: "Casual Standard 3v3",
    4: "Casual Chaos 4v4",
    6: "Private Match",
    10: "Ranked Duel 1v1",
    11: "Ranked Doubles 2v2",
    13: "Ranked Standard 3v3",
    27: "Hoops",
    28: "Rumble",
    29: "Dropshot",
    30: "Snowday",
    34: "Tournament",
    61: "Ranked 4v4 Quads",
    63: "Heatseeker",
}

# Only these playlists have MMR tracked by the app.
TRACKED_PLAYLISTS = (10, 11, 13)


def playlist_name(playlist_id: int) -> str:
    return PLAYLISTS.get(playlist_id, f"Playlist {playlist_id}")


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))  # tolerate a BOM from editors
    except (FileNotFoundError, json.JSONDecodeError):
        return cfg
    if isinstance(user, dict):
        for old in OBSOLETE_KEYS:  # features removed in later versions
            user.pop(old, None)
        for section, keys in OBSOLETE_SUBKEYS.items():
            if isinstance(user.get(section), dict):
                for key in keys:
                    user[section].pop(key, None)
        _merge(cfg, user)
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ====================================================================================================
# History: Parsing and shaping of MMR history. No network or database access here.
# ====================================================================================================


SOURCE_TRACKER_IMPORT = "tracker_import"  # recorded snapshot from a Tracker history export
SOURCE_CURRENT = "kmdw_current"           # reading from the current-MMR API
SOURCE_MANUAL = "manual"                  # MMR the user typed in (e.g. from the in-game display)
SOURCE_SCREEN = "screen"                  # MMR read from the in-game display by OCR (old data only)
SOURCE_SHIFT = "shift"                    # MMR shown by Shift (its tracker.ini or its Tracker > Match card)
SOURCE_RL_LOG = "rl_log"                  # MMR from Rocket League's Launch.log when you queue (rllog.py)
SOURCE_PSYNET = "psynet"                  # MMR read live from Rocket League's servers after an Epic sign-in (psynet.py)
USER_SOURCES = (SOURCE_MANUAL, SOURCE_SCREEN, SOURCE_SHIFT)  # readings that settle matches waiting for MMR


class HistoryFormatError(ValueError):
    """The payload is not shaped like the Tracker history response."""


@dataclass(frozen=True)
class Snapshot:
    playlist_id: int
    rating: int
    tier: str
    division: str
    tier_id: int | None
    division_id: int | None
    recorded_at: datetime  # always timezone-aware, UTC
    source: str = SOURCE_TRACKER_IMPORT


def parse_timestamp(text: str) -> datetime:
    """ISO-8601 to aware UTC. A timestamp without an offset is taken as UTC."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty timestamp")
    parsed = datetime.fromisoformat(text.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _parse_record(playlist_id: int, record) -> Snapshot | None:
    if not isinstance(record, dict):
        return None
    rating = _as_int(record.get("rating"))
    if rating is None:
        return None
    try:
        recorded_at = parse_timestamp(record.get("collectDate"))
    except ValueError:
        return None
    return Snapshot(
        playlist_id=playlist_id,
        rating=rating,
        tier=str(record.get("tier") or ""),
        division=str(record.get("division") or ""),
        tier_id=_as_int(record.get("tierId")),
        division_id=_as_int(record.get("divisionId")),
        recorded_at=recorded_at,
    )


def parse_tracker_history(payload) -> tuple[list[Snapshot], int]:
    """Parse a Tracker player-history response.

    Returns (snapshots sorted by playlist then time, number of records skipped).
    Malformed records are skipped and counted rather than failing the whole import.
    Identical records are kept here; the database de-duplicates on import.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise HistoryFormatError("expected an object with a 'data' object")

    snapshots: list[Snapshot] = []
    skipped = 0
    for key, records in payload["data"].items():
        try:
            playlist_id = int(key)
        except (TypeError, ValueError):
            skipped += len(records) if isinstance(records, list) else 1
            continue
        if not isinstance(records, list):
            skipped += 1
            continue
        for record in records:
            snap = _parse_record(playlist_id, record)
            if snap is None:
                skipped += 1
            else:
                snapshots.append(snap)

    snapshots.sort(key=lambda s: (s.playlist_id, s.recorded_at))
    return snapshots, skipped


def aggregate(points: list[tuple[datetime, int]], granularity: str) -> list[tuple[datetime, int]]:
    """Reduce observations to one per bucket, keeping the real timestamp of the last one.

    granularity: 'raw' (no change), 'daily', 'weekly' (ISO week) or 'monthly'.
    Timestamps are never invented: each bucket reports the actual time of its last reading.
    """
    if granularity == "raw":
        return list(points)
    keyers = {
        "daily": lambda t: t.date(),
        "weekly": lambda t: t.isocalendar()[:2],
        "monthly": lambda t: (t.year, t.month),
    }
    if granularity not in keyers:
        raise ValueError(f"unknown granularity: {granularity}")
    keyer = keyers[granularity]
    ordered = sorted(points, key=lambda p: p[0])
    last_per_bucket: dict = {}
    for when, rating in ordered:
        last_per_bucket[keyer(when)] = (when, rating)  # later readings overwrite earlier ones
    return sorted(last_per_bucket.values(), key=lambda p: p[0])


# ====================================================================================================
# Streaks: Win/loss arithmetic. Pure functions over results ordered oldest first (True = win).
# ====================================================================================================

def current_streak(results: list[bool]) -> int:
    """Positive for a win streak, negative for a loss streak, 0 if no results."""
    if not results:
        return 0
    last = results[-1]
    count = 0
    for won in reversed(results):
        if won != last:
            break
        count += 1
    return count if last else -count


def longest_streaks(results: list[bool]) -> tuple[int, int]:
    """(longest win streak, longest loss streak)."""
    best_win = best_loss = run = 0
    prev = None
    for won in results:
        run = run + 1 if won == prev else 1
        prev = won
        if won:
            best_win = max(best_win, run)
        else:
            best_loss = max(best_loss, run)
    return best_win, best_loss


def summarize(results: list[bool]) -> dict:
    wins = sum(1 for r in results if r)
    losses = len(results) - wins
    longest_win, longest_loss = longest_streaks(results)
    return {
        "wins": wins,
        "losses": losses,
        "win_ratio": (wins / len(results)) if results else None,
        "current_streak": current_streak(results),
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
    }


# ====================================================================================================
# Sessions: Group recorded matches into play sessions. Pure functions over match rows from the database.
# ====================================================================================================


@dataclass
class PlaylistSession:
    wins: int = 0
    losses: int = 0
    mmr_delta: int = 0
    logged_deltas: int = 0  # matches whose MMR change was read; others count toward W/L only


@dataclass
class Session:
    start: datetime
    end: datetime
    results: list[bool] = field(default_factory=list)
    playlists: dict[int, PlaylistSession] = field(default_factory=dict)

    @property
    def summary(self) -> dict:
        return summarize(self.results)


def split_sessions(matches: list[dict], gap: timedelta) -> list[Session]:
    """Matches separated by more than `gap` start a new session. Returns newest session first.

    matches: rows with 'ended_at' (ISO-8601), 'won', 'playlist_id' and 'mmr_delta' (may be None).
    """
    ordered = sorted(matches, key=lambda m: m["ended_at"])
    sessions: list[Session] = []
    for m in ordered:
        when = datetime.fromisoformat(m["ended_at"])
        if not sessions or when - sessions[-1].end > gap:
            sessions.append(Session(start=when, end=when))
        s = sessions[-1]
        s.end = when
        won = bool(m["won"])
        s.results.append(won)
        p = s.playlists.setdefault(m["playlist_id"], PlaylistSession())
        if won:
            p.wins += 1
        else:
            p.losses += 1
        if m.get("mmr_delta") is not None:
            p.mmr_delta += m["mmr_delta"]
            p.logged_deltas += 1
    sessions.reverse()
    return sessions


# ====================================================================================================
# Ranks: Rank names and rank-change detection. Pure functions, no network or database access.
#
# Tier and division IDs share one scale across sources: Tracker imports and the MMR API
# both store tierId 12 / divisionId 1 for Platinum III Division II (checked against stored data).
# ====================================================================================================


_METALS = ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Champion", "Grand Champion"]
TIERS = ["Unranked"] + [f"{m} {n}" for m in _METALS for n in ("I", "II", "III")] + ["Supersonic Legend"]
DIVISIONS = ["Division I", "Division II", "Division III", "Division IV"]


def tier_name(tier_id: int | None) -> str:
    if tier_id is None:
        return ""
    return TIERS[tier_id] if 0 <= tier_id < len(TIERS) else f"Tier {tier_id}"


def division_name(division_id: int | None) -> str:
    if division_id is None:
        return ""
    return DIVISIONS[division_id] if 0 <= division_id < len(DIVISIONS) else f"Division {division_id + 1}"


def rank_label(tier_id: int | None, division_id: int | None, tier: str = "", division: str = "") -> str:
    """Readable rank. Stored names win; IDs are the fallback (MMR API readings have IDs only)."""
    t = tier or tier_name(tier_id)
    if not t:
        return ""
    if tier_id in (0, len(TIERS) - 1):  # Unranked and SSL have no divisions
        return t
    d = division or division_name(division_id)
    return f"{t} {d.replace('Division ', 'Div ')}" if d else t


def next_division(tier_id: int, division_id: int) -> tuple[int, int] | None:
    if tier_id >= len(TIERS) - 1:  # SSL is the top
        return None
    if tier_id == 0:
        return None  # unranked: placement matches decide
    if division_id < len(DIVISIONS) - 1:
        return tier_id, division_id + 1
    return tier_id + 1, 0


def prev_division(tier_id: int, division_id: int) -> tuple[int, int] | None:
    if tier_id <= 1 and division_id == 0:
        return None
    if tier_id == len(TIERS) - 1:
        return tier_id - 1, len(DIVISIONS) - 1
    if division_id > 0:
        return tier_id, division_id - 1
    return tier_id - 1, len(DIVISIONS) - 1


@dataclass(frozen=True)
class RankDistance:
    """MMR to the next and previous division."""
    next_label: str | None
    to_next: int | None      # MMR still needed (at least 1); None when unknown
    prev_label: str | None
    to_drop: int | None      # MMR you can lose and stay in this division; None when unknown
    estimated: bool = True   # True: from your own readings; False: from a rank table


# --- rank tables (division MMR windows) --------------------------------------

Band = tuple[int, int, int, int]  # (tier_id, division_id, min_mmr, max_mmr)
BUNDLED_THRESHOLDS = Path(__file__).resolve().parent / "assets" / "rank_thresholds.json"


def parse_tracker_thresholds(payload) -> dict[int, list[Band]]:
    """Tracker's rank-distribution response ({"data": {"data": [{tier, division, playlist, minMMR, maxMMR}]}})
    or our own file format ({"playlists": {"11": [[tier, div, min, max], ...]}})."""
    out: dict[int, list[Band]] = {}
    if isinstance(payload, dict) and isinstance(payload.get("playlists"), dict):
        for key, rows in payload["playlists"].items():
            bands = [tuple(int(v) for v in r[:4]) for r in rows if isinstance(r, list) and len(r) >= 4]
            if bands:
                out[int(key)] = bands
        return out
    inner = payload.get("data") if isinstance(payload, dict) else None
    rows = inner.get("data") if isinstance(inner, dict) else None
    if not isinstance(rows, list):
        raise ValueError("expected Tracker rank data: {\"data\": {\"data\": [...]}}")
    for r in rows:
        try:
            band = (int(r["tier"]), int(r["division"]), int(r["minMMR"]), int(r["maxMMR"]))
            out.setdefault(int(r["playlist"]), []).append(band)
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise ValueError("no usable rank rows found")
    return out


def load_thresholds(user_file: Path | None = None) -> dict[int, list[Band]]:
    """Bundled tables, overridden per playlist by any the user imported."""
    tables: dict[int, list[Band]] = {}
    for path in (BUNDLED_THRESHOLDS, user_file):
        if path is None:
            continue
        try:
            tables.update(parse_tracker_thresholds(json.loads(Path(path).read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    for pid in tables:
        tables[pid] = sorted((b for b in tables[pid] if b[0] > 0), key=lambda b: b[2])  # drop Unranked
    return tables


def rank_for_mmr(table: list[Band], mmr: int) -> tuple[int, int] | None:
    """The division whose window starts at or below mmr. Windows can leave small gaps; a rating in a
    gap belongs to the lower division because the next one isn't reached yet."""
    found = None
    for tier, div, low, _high in table:
        if mmr >= low:
            found = (tier, div)
        else:
            break
    return found


def table_distance(rating: int, table: list[Band], tier_id: int | None = None,
                   division_id: int | None = None) -> RankDistance | None:
    current = (tier_id, division_id or 0) if tier_id is not None else rank_for_mmr(table, rating)
    if current is None:
        return None
    starts = {(t, d): low for t, d, low, _ in table}
    nxt = next_division(*current)
    prv = prev_division(*current)
    to_next = max(1, starts[nxt] - rating) if nxt in starts else None
    to_drop = max(0, rating - starts[current]) if current in starts and prv else None
    return RankDistance(rank_label(*nxt) if nxt else None, to_next,
                        rank_label(*prv) if prv else None, to_drop, estimated=False)


def rank_distance(rating: int, tier_id: int | None, division_id: int | None,
                  readings: list[tuple[int, int | None, int | None]],
                  table: list[Band] | None = None) -> RankDistance | None:
    """Uses the rank table when there is one; otherwise estimates from readings.

    readings: (rating, tier_id, division_id) from one playlist, ideally the current season. The
    estimate takes the lowest rating ever seen in a division as its threshold.
    """
    if table:
        return table_distance(rating, table, tier_id, division_id)
    if tier_id is None:
        return None
    div = division_id or 0
    lowest: dict[tuple[int, int], int] = {}
    for r, t, d in readings:
        if t is None:
            continue
        key = (t, d or 0)
        lowest[key] = min(r, lowest.get(key, r))
    nxt = next_division(tier_id, div)
    to_next = None
    if nxt and nxt in lowest:
        to_next = max(1, lowest[nxt] - rating)
    to_drop = None
    if (tier_id, div) in lowest and prev_division(tier_id, div):
        to_drop = max(0, rating - lowest[(tier_id, div)])
    prv = prev_division(tier_id, div)
    return RankDistance(
        next_label=rank_label(*nxt) if nxt else None,
        to_next=to_next,
        prev_label=rank_label(*prv) if prv else None,
        to_drop=to_drop,
    )


def tier_distance(rating: int, tier_id: int | None, readings: list[tuple[int, int | None, int | None]],
                  table: list[Band] | None = None) -> RankDistance | None:
    """Like rank_distance, but by whole rank (Diamond III -> Champion I), ignoring divisions.

    to_next: MMR still needed to reach the next rank's first division. to_drop: MMR you can lose before
    falling below this rank's first division. With a rank table the starts are exact; without one, the
    lowest rating seen in a rank stands in for its start (estimated).
    """
    starts: dict[int, int] = {}
    if table:
        for t, _d, low, _high in table:
            starts[t] = min(low, starts.get(t, low))
        # the rank the reading says wins: the table's windows can be a few MMR off the game's
        current = tier_id if tier_id else (rank_for_mmr(table, rating) or (tier_id, 0))[0]
    else:
        for r, t, _d in readings:
            if t is not None and t > 0:
                starts[t] = min(r, starts.get(t, r))
        current = tier_id
    if current is None or current <= 0:
        return None
    nxt = current + 1 if current < len(TIERS) - 1 else None
    prv = current - 1 if current > 1 else None
    return RankDistance(
        next_label=tier_name(nxt) if nxt else None,
        to_next=max(1, starts[nxt] - rating) if nxt in starts else None,
        prev_label=tier_name(prv) if prv else None,
        to_drop=max(0, rating - starts[current]) if prv and current in starts else None,
        estimated=not table,
    )


@dataclass(frozen=True)
class RankChange:
    when: datetime
    rating: int
    before: str
    after: str
    direction: int  # +1 promotion, -1 demotion
    tier_changed: bool


def rank_changes(rows: list[tuple[datetime, int, int | None, int | None, str, str]]) -> list[RankChange]:
    """Changes in (tier, division) between consecutive readings.

    rows: (recorded_at, rating, tier_id, division_id, tier, division), any order.
    Readings without a tier ID are skipped. Since readings are snapshots with gaps,
    a change is reported at the first reading that shows the new rank, not the match that caused it.
    """
    ordered = sorted((r for r in rows if r[2] is not None), key=lambda r: r[0])
    out: list[RankChange] = []
    prev = None
    for when, rating, tier_id, division_id, tier, division in ordered:
        div = division_id if division_id is not None else 0
        key = (tier_id, div)
        if prev is not None and key != prev[0]:
            out.append(RankChange(
                when=when,
                rating=rating,
                before=prev[1],
                after=rank_label(tier_id, division_id, tier, division),
                direction=1 if key > prev[0] else -1,
                tier_changed=tier_id != prev[0][0],
            ))
        prev = (key, rank_label(tier_id, division_id, tier, division))
    return out


# ====================================================================================================
# Insights: Per-match stat summaries and time-of-day win rates. Pure functions over match rows.
# ====================================================================================================


CORE_STATS = ("score", "goals", "assists", "saves", "shots", "demos", "touches")
STAT_LABELS = {"score": "Score", "goals": "Goals", "assists": "Assists", "saves": "Saves",
               "shots": "Shots", "demos": "Demos", "touches": "Touches"}


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stat_averages(matches: list[dict]) -> dict[str, float | None]:
    """Average of each core stat over the matches that recorded it."""
    return {s: _avg([m[s] for m in matches if m.get(s) is not None]) for s in CORE_STATS}


def wins_vs_losses(matches: list[dict]) -> dict[str, tuple[float | None, float | None]]:
    wins = [m for m in matches if m["won"]]
    losses = [m for m in matches if not m["won"]]
    w, lo = stat_averages(wins), stat_averages(losses)
    return {s: (w[s], lo[s]) for s in CORE_STATS}


def with_stats(matches: list[dict]) -> list[dict]:
    return [m for m in matches if any(m.get(s) is not None for s in CORE_STATS)]


def heatmap(matches: list[dict], tz=None) -> list[list[tuple[int, int]]]:
    """7 x 24 grid [weekday Monday=0][hour] of (wins, games), in local time."""
    grid = [[(0, 0) for _ in range(24)] for _ in range(7)]
    for m in matches:
        t = datetime.fromisoformat(m["ended_at"]).astimezone(tz)
        w, g = grid[t.weekday()][t.hour]
        grid[t.weekday()][t.hour] = (w + (1 if m["won"] else 0), g + 1)
    return grid


def best_hours(grid: list[list[tuple[int, int]]], min_games: int = 5) -> list[tuple[int, float, int]]:
    """(hour, win rate, games) across all weekdays, best first, hours with enough games only."""
    out = []
    for hour in range(24):
        wins = sum(grid[d][hour][0] for d in range(7))
        games = sum(grid[d][hour][1] for d in range(7))
        if games >= min_games:
            out.append((hour, wins / games, games))
    return sorted(out, key=lambda x: (-x[1], -x[2]))


# ====================================================================================================
# Db: SQLite storage. Each call opens its own connection, so the overlay and the app can share it.
# ====================================================================================================


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    key        TEXT PRIMARY KEY,      -- label chosen by the user
    player_id  TEXT,                  -- e.g. Epic|<32 hex>|0 (needed for current MMR)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mmr_snapshots (
    id           INTEGER PRIMARY KEY,
    profile_key  TEXT NOT NULL REFERENCES profiles(key),
    playlist_id  INTEGER NOT NULL,
    rating       INTEGER NOT NULL,
    tier         TEXT NOT NULL DEFAULT '',
    division     TEXT NOT NULL DEFAULT '',
    tier_id      INTEGER,
    division_id  INTEGER,
    recorded_at  TEXT NOT NULL,       -- ISO-8601, UTC
    source       TEXT NOT NULL,
    -- Same reading imported twice is ignored; distinct readings at the same time are kept.
    UNIQUE (profile_key, playlist_id, recorded_at, source, rating)
);
CREATE INDEX IF NOT EXISTS ix_snapshots ON mmr_snapshots (profile_key, playlist_id, recorded_at);
CREATE TABLE IF NOT EXISTS matches (
    id           INTEGER PRIMARY KEY,
    profile_key  TEXT NOT NULL REFERENCES profiles(key),
    match_key    TEXT NOT NULL,       -- MatchGuid, or a generated key for offline matches
    playlist_id  INTEGER NOT NULL,
    won          INTEGER NOT NULL,    -- 1 = win, 0 = loss
    ended_at     TEXT NOT NULL,       -- ISO-8601, UTC
    mmr_before   INTEGER,
    mmr_after    INTEGER,
    mmr_delta    INTEGER,
    UNIQUE (profile_key, match_key)
);
CREATE INDEX IF NOT EXISTS ix_matches ON matches (profile_key, playlist_id, ended_at);
CREATE TABLE IF NOT EXISTS screen_reads (
    id           INTEGER PRIMARY KEY,
    profile_key  TEXT NOT NULL,
    playlist_id  INTEGER NOT NULL,
    value        INTEGER NOT NULL,     -- MMR the OCR found
    line         TEXT NOT NULL DEFAULT '',  -- the screen text it came from
    read_at      TEXT NOT NULL,        -- ISO-8601, UTC
    status       TEXT NOT NULL DEFAULT 'pending'  -- pending | saved | skipped
);
CREATE TABLE IF NOT EXISTS games (            -- one per finished match, from the Stats API
    id           INTEGER PRIMARY KEY,
    profile_key  TEXT NOT NULL,
    match_guid   TEXT NOT NULL,
    playlist_id  INTEGER NOT NULL,
    arena        TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,       -- ISO-8601, UTC
    ended_at     TEXT NOT NULL,
    won          INTEGER NOT NULL,    -- 1 = win, 0 = loss
    my_team      INTEGER NOT NULL,    -- 0 blue, 1 orange
    team_goals   INTEGER,
    opp_goals    INTEGER,
    overtime     INTEGER NOT NULL DEFAULT 0,
    forfeit      INTEGER NOT NULL DEFAULT 0,  -- ended with time left, outside overtime
    UNIQUE (profile_key, match_guid)
);
CREATE INDEX IF NOT EXISTS ix_games ON games (profile_key, ended_at);
CREATE TABLE IF NOT EXISTS game_players (     -- everyone in a game, with their final stats
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL,    -- you first, then teammates, then opponents
    name         TEXT NOT NULL,
    primary_id   TEXT NOT NULL DEFAULT '',  -- Platform|Uid|Splitscreen
    team         INTEGER NOT NULL,
    is_self      INTEGER NOT NULL,
    teammate     INTEGER NOT NULL,
    score        INTEGER NOT NULL DEFAULT 0,
    goals        INTEGER NOT NULL DEFAULT 0,
    shots        INTEGER NOT NULL DEFAULT 0,
    assists      INTEGER NOT NULL DEFAULT 0,
    saves        INTEGER NOT NULL DEFAULT 0,
    touches      INTEGER NOT NULL DEFAULT 0,
    car_touches  INTEGER NOT NULL DEFAULT 0,
    demos        INTEGER NOT NULL DEFAULT 0,
    demoed       INTEGER NOT NULL DEFAULT 0,  -- times demolished (from StatfeedEvent)
    boost_avg    REAL,                -- average boost; only for cars the game sends boost for
    left_early   INTEGER NOT NULL DEFAULT 0,
    feed         TEXT NOT NULL DEFAULT '{}',  -- stat feed counts, e.g. {"EpicSave": 1}
    PRIMARY KEY (game_id, slot)
);
"""

# Columns added after the first release. Missing ones are added to existing databases on connect.
MATCH_COLUMNS = {
    "mmr_matches": "INTEGER",   # matches covered by mmr_delta; NULL or 1 = this match alone
    "score": "INTEGER",         # per-match stats from the Stats API (own player)
    "goals": "INTEGER",
    "assists": "INTEGER",
    "saves": "INTEGER",
    "shots": "INTEGER",
    "demos": "INTEGER",
    "touches": "INTEGER",
    "team_goals": "INTEGER",
    "opp_goals": "INTEGER",
    "overtime": "INTEGER",
}
STAT_FIELDS = ("score", "goals", "assists", "saves", "shots", "demos", "touches",
               "team_goals", "opp_goals", "overtime")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    for name, kind in MATCH_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {name} {kind}")
    return conn


def upsert_profile(key: str, player_id: str | None = None, path: Path | None = None) -> None:
    with closing(connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO profiles (key, player_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET player_id = COALESCE(excluded.player_id, profiles.player_id)",
            (key, player_id, utc_now_iso()),
        )


def set_profile_player(key: str, player_id: str | None, path: Path | None = None) -> None:
    with closing(connect(path)) as conn, conn:
        conn.execute("UPDATE profiles SET player_id = ? WHERE key = ?", (player_id, key))


PROFILE_TABLES = ("mmr_snapshots", "matches", "screen_reads", "games")
PROFILE_RENAMED: list = []  # callbacks (old key, new key) after profile_for_epic_account repaired a name


def looks_garbled(name: str) -> bool:
    """A name that went through a non-UTF-8 step: '?' or U+FFFD where letters were (Epic names can't hold
    either)."""
    return chr(0xFFFD) in name or "?" in name


def rename_profile(old: str, new: str, path: Path | None = None) -> bool:
    """Give a profile a new key, moving everything stored under it. False when `new` is taken or `old` is gone."""
    new = new.strip()
    if not new or new == old:
        return False
    with closing(connect(path)) as conn, conn:
        row = conn.execute("SELECT player_id, created_at FROM profiles WHERE key = ?", (old,)).fetchone()
        if row is None or conn.execute("SELECT 1 FROM profiles WHERE key = ?", (new,)).fetchone():
            return False
        conn.execute("INSERT INTO profiles (key, player_id, created_at) VALUES (?, ?, ?)",
                     (new, row["player_id"], row["created_at"]))
        for table in PROFILE_TABLES:
            conn.execute(f"UPDATE {table} SET profile_key = ? WHERE profile_key = ?", (new, old))
        conn.execute("DELETE FROM profiles WHERE key = ?", (old,))
    return True


def list_profiles(path: Path | None = None) -> list[dict]:
    with closing(connect(path)) as conn:
        rows = conn.execute("SELECT key, player_id FROM profiles ORDER BY key").fetchall()
        return [dict(r) for r in rows]


def profile_for_epic_account(account_id: str, name: str, path: Path | None = None) -> str:
    """The key of the profile for an Epic account (32-hex id), added the first time (named after the account).
    A profile with that name and no account yet (made in setup by typing an Epic name) gets this account."""
    player_id = f"Epic|{account_id.lower()}|0"
    profiles = list_profiles(path)
    for p in profiles:
        if (p["player_id"] or "").lower() == player_id.lower():
            clean = name.strip()
            if clean and looks_garbled(p["key"]) and not looks_garbled(clean) and rename_profile(p["key"], clean, path):
                for hook in PROFILE_RENAMED:  # the app moves its settings for the profile along
                    hook(p["key"], clean)
                return clean
            return p["key"]
    unclaimed = next((p for p in profiles if not p["player_id"] and name.strip()
                      and p["key"].lower() == name.strip().lower()), None)
    if unclaimed:
        set_profile_player(unclaimed["key"], player_id, path)
        return unclaimed["key"]
    key = name.strip() or f"Epic {account_id[:6]}"
    if get_profile(key, path):  # that name is taken by another player's profile
        key = f"{key} ({account_id[:6]})"
    upsert_profile(key, player_id, path)
    return key


def profile_for_player(primary_id: str, name: str, path: Path | None = None) -> str:
    """The profile of a Stats API player (PrimaryId 'Epic|<hex>|0', 'Steam|<id>|0', ...), matched on platform
    and id; added the first time, named after the player."""
    parts = (primary_id or "").split("|")
    if len(parts) >= 2 and parts[0].lower() == "epic" and len(parts[1]) == 32:
        return profile_for_epic_account(parts[1], name, path)
    want = "|".join(parts[:2]).lower()
    for p in list_profiles(path):
        if "|".join((p["player_id"] or "").split("|")[:2]).lower() == want and want:
            return p["key"]
    key = name.strip() or (primary_id or "Player")
    if get_profile(key, path):
        key = f"{key} ({parts[1][:6] if len(parts) > 1 else 'new'})"
    upsert_profile(key, primary_id or None, path)
    return key


def get_profile(key: str, path: Path | None = None) -> dict | None:
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT key, player_id FROM profiles WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def insert_snapshots(profile_key: str, snapshots: list[Snapshot], path: Path | None = None) -> int:
    """Insert snapshots. Returns how many were new (duplicates are ignored)."""
    rows = [
        (profile_key, s.playlist_id, s.rating, s.tier, s.division, s.tier_id, s.division_id,
         s.recorded_at.isoformat(), s.source)
        for s in snapshots
    ]
    with closing(connect(path)) as conn, conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO mmr_snapshots "
            "(profile_key, playlist_id, rating, tier, division, tier_id, division_id, recorded_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before


def list_snapshots(profile_key: str, playlist_id: int, path: Path | None = None) -> list[dict]:
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT playlist_id, rating, tier, division, tier_id, division_id, recorded_at, source "
            "FROM mmr_snapshots WHERE profile_key = ? AND playlist_id = ? ORDER BY recorded_at",
            (profile_key, playlist_id),
        ).fetchall()
        return [dict(r) for r in rows]


def all_snapshots(profile_key: str, path: Path | None = None) -> list[dict]:
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT playlist_id, rating, tier, division, tier_id, division_id, recorded_at, source "
            "FROM mmr_snapshots WHERE profile_key = ? ORDER BY playlist_id, recorded_at",
            (profile_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def playlists_with_snapshots(profile_key: str, path: Path | None = None) -> list[int]:
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT playlist_id FROM mmr_snapshots WHERE profile_key = ? ORDER BY playlist_id",
            (profile_key,),
        ).fetchall()
        return [r["playlist_id"] for r in rows]


def latest_rating(profile_key: str, playlist_id: int, path: Path | None = None) -> int | None:
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT rating FROM mmr_snapshots WHERE profile_key = ? AND playlist_id = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (profile_key, playlist_id),
        ).fetchone()
        return row["rating"] if row else None


def insert_match(profile_key: str, match_key: str, playlist_id: int, won: bool,
                 ended_at: str, stats: dict | None = None, path: Path | None = None) -> int | None:
    """Store one match result. Returns the new row id, or None if it was already stored."""
    stats = stats or {}
    cols = [f for f in STAT_FIELDS if stats.get(f) is not None]
    values = [int(stats[f]) for f in cols]
    names = ", ".join(["profile_key", "match_key", "playlist_id", "won", "ended_at", *cols])
    marks = ", ".join("?" * (5 + len(cols)))
    with closing(connect(path)) as conn, conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO matches ({names}) VALUES ({marks})",
            (profile_key, match_key, playlist_id, int(won), ended_at, *values),
        )
        return cur.lastrowid if cur.rowcount else None


def set_match_mmr(match_id: int, mmr_before: int | None, mmr_after: int | None,
                  covered: int = 1, path: Path | None = None) -> None:
    """covered > 1 means the change spans this match and the waiting matches before it."""
    delta = None if mmr_before is None or mmr_after is None else mmr_after - mmr_before
    with closing(connect(path)) as conn, conn:
        conn.execute(
            "UPDATE matches SET mmr_before = ?, mmr_after = ?, mmr_delta = ?, mmr_matches = ? WHERE id = ?",
            (mmr_before, mmr_after, delta, covered if covered > 1 else None, match_id),
        )


def latest_snapshot(profile_key: str, playlist_id: int, source: str | tuple[str, ...] | None = None,
                    path: Path | None = None) -> dict | None:
    sql = ("SELECT rating, tier_id, division_id, recorded_at, source FROM mmr_snapshots "
           "WHERE profile_key = ? AND playlist_id = ?")
    args: list = [profile_key, playlist_id]
    if source:
        sources = (source,) if isinstance(source, str) else tuple(source)
        sql += f" AND source IN ({', '.join('?' * len(sources))})"
        args.extend(sources)
    with closing(connect(path)) as conn:
        row = conn.execute(sql + " ORDER BY recorded_at DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None


def snapshot_before(profile_key: str, playlist_id: int, before: datetime, source: str,
                    path: Path | None = None) -> dict | None:
    """The newest reading from `source` taken before `before` (strictly), or None."""
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT rating, recorded_at FROM mmr_snapshots WHERE profile_key = ? AND playlist_id = ? AND source = ?",
            (profile_key, playlist_id, source),
        ).fetchall()
    # compared as datetimes: stored ISO strings can carry different offsets
    older = [(datetime.fromisoformat(r["recorded_at"]), dict(r)) for r in rows]
    older = [(t, r) for t, r in older if t < before]
    return max(older, key=lambda p: p[0])[1] if older else None


def list_matches(profile_key: str, playlist_id: int | None = None, path: Path | None = None) -> list[dict]:
    with closing(connect(path)) as conn:
        if playlist_id is None:
            rows = conn.execute(
                "SELECT * FROM matches WHERE profile_key = ? ORDER BY ended_at", (profile_key,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM matches WHERE profile_key = ? AND playlist_id = ? ORDER BY ended_at",
                (profile_key, playlist_id),
            ).fetchall()
        return [dict(r) for r in rows]


GAME_PLAYER_STATS = ("score", "goals", "shots", "assists", "saves", "touches", "car_touches", "demos", "demoed")


def save_game(profile_key: str, game, path: Path | None = None) -> int | None:
    """Store a finished game (a stats.GameRecord) and everyone's stats. Returns the new id, or None if that
    match was already stored for this profile."""
    with closing(connect(path)) as conn, conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO games (profile_key, match_guid, playlist_id, arena, started_at, ended_at, won, "
            "my_team, team_goals, opp_goals, overtime, forfeit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (profile_key, game.match_guid, game.playlist_id, game.arena, game.started_at.isoformat(),
             game.ended_at.isoformat(), int(game.won), game.my_team, game.team_goals, game.opp_goals,
             int(game.overtime), int(game.forfeit)))
        if not cur.rowcount:
            return None
        game_id = cur.lastrowid
        cols = ("game_id", "slot", "name", "primary_id", "team", "is_self", "teammate", *GAME_PLAYER_STATS,
                "boost_avg", "left_early", "feed")
        conn.executemany(
            f"INSERT INTO game_players ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [(game_id, slot, p["name"], p.get("primary_id") or "", p["team"], int(p["is_self"]), int(p["teammate"]),
              *(int(p.get(s) or 0) for s in GAME_PLAYER_STATS), p.get("boost_avg"), int(bool(p.get("left_early"))),
              json.dumps(p.get("feed") or {}))
             for slot, p in enumerate(game.players)])
        return game_id


def list_games(profile_key: str, playlist_id: int | None = None, path: Path | None = None) -> list[dict]:
    """Saved games, newest first, each with "players" (you, teammates, opponents)."""
    with closing(connect(path)) as conn:
        sql, args = "SELECT * FROM games WHERE profile_key = ?", [profile_key]
        if playlist_id is not None:
            sql += " AND playlist_id = ?"
            args.append(playlist_id)
        games = [dict(r) for r in conn.execute(sql + " ORDER BY ended_at DESC", args)]
        by_id = {g["id"]: g for g in games}
        for g in games:
            g["players"] = []
        if games:
            marks = ", ".join("?" * len(by_id))
            for r in conn.execute(f"SELECT * FROM game_players WHERE game_id IN ({marks}) ORDER BY game_id, slot",
                                  list(by_id)):
                p = dict(r)
                p["is_self"], p["teammate"], p["left_early"] = bool(p["is_self"]), bool(p["teammate"]), bool(p["left_early"])
                try:
                    p["feed"] = json.loads(p["feed"] or "{}")
                except ValueError:
                    p["feed"] = {}
                by_id[p["game_id"]]["players"].append(p)
        return games


def game_playlists(profile_key: str, path: Path | None = None) -> list[tuple[int, int]]:
    """(playlist id, games) for the playlists with saved games, most played first."""
    with closing(connect(path)) as conn:
        rows = conn.execute("SELECT playlist_id, COUNT(*) AS n FROM games WHERE profile_key = ? "
                            "GROUP BY playlist_id ORDER BY n DESC, playlist_id", (profile_key,)).fetchall()
        return [(r["playlist_id"], r["n"]) for r in rows]


def delete_profile(key: str, path: Path | None = None) -> None:
    """Remove a profile and everything stored for it (readings, games)."""
    with closing(connect(path)) as conn, conn:
        conn.execute("DELETE FROM game_players WHERE game_id IN (SELECT id FROM games WHERE profile_key = ?)", (key,))
        for table in ("games", "mmr_snapshots", "matches", "screen_reads", "profiles"):
            conn.execute(f"DELETE FROM {table} WHERE {'key' if table == 'profiles' else 'profile_key'} = ?", (key,))


def delete_game(game_id: int, path: Path | None = None) -> None:
    with closing(connect(path)) as conn, conn:
        conn.execute("DELETE FROM game_players WHERE game_id = ?", (game_id,))
        conn.execute("DELETE FROM games WHERE id = ?", (game_id,))


# ====================================================================================================
# Mmr Ledger: Matches waiting for MMR, settled by the MMR readings you save (screen readings or typed in).
#
# There is no MMR service: after a ranked match the overlay reads the game's MMR display, and every
# saved reading closes out the matches played in that playlist since the previous reading:
# - one waiting match gets its exact change;
# - several waiting matches get one combined change, stored on the last of them with the number of
#   matches it covers, because a single reading can't be split per match.
# Waiting matches are rebuilt from the database on start, so restarting the overlay loses nothing.
# ====================================================================================================


STALE = timedelta(hours=3)  # unread matches older than this belong to a past session


@dataclass
class Waiting:
    """Matches in one playlist waiting for a reading, and the rating before the first of them."""
    before: int | None
    matches: list[tuple[int, datetime]] = field(default_factory=list)  # (match id, ended_at)


def settle(waiting: Waiting, after: int) -> list[tuple[int, int | None, int, int]]:
    """Updates to store once a new rating is known: (match id, before, after, matches covered).

    One waiting match: exact before/after. Several: the combined change goes on the last match.
    """
    if not waiting.matches:
        return []
    last_id = waiting.matches[-1][0]
    return [(last_id, waiting.before, after, len(waiting.matches))]


class MmrLedger:
    def __init__(self, profile_key: str, db_path: Path | None = None, now=lambda: datetime.now(timezone.utc)):
        self.profile_key = profile_key
        self.db_path = db_path
        self.now = now
        self.last_rating: dict[int, int] = {}
        self.waiting: dict[int, Waiting] = {}
        self.unknown_baseline: set[int] = set()  # playlists whose MMR moved by an unknown amount since the last reading
        self.loaded_updates = self.load()

    def load(self) -> list[dict]:
        """Rebuild state from the database and settle what stored readings already explain.

        Matches are grouped by the readings just before and just after them. A group where no match
        has MMR yet and the two readings differ gets the combined change; matches after the latest
        reading are left waiting. Returns the updates made.
        """
        self.last_rating.clear()
        self.waiting.clear()
        self.unknown_baseline.clear()
        updates = []
        for pid in TRACKED_PLAYLISTS:
            readings = [(datetime.fromisoformat(r["recorded_at"]), r["rating"])
                        for r in list_snapshots(self.profile_key, pid, path=self.db_path)]
            if readings:
                self.last_rating[pid] = readings[-1][1]
            groups: dict[tuple[int | None, int | None], list[dict]] = {}
            for m in list_matches(self.profile_key, pid, path=self.db_path):
                ended = datetime.fromisoformat(m["ended_at"])
                before = max((i for i, (t, _) in enumerate(readings) if t < ended), default=None)
                after = min((i for i, (t, _) in enumerate(readings) if t > ended), default=None)
                groups.setdefault((before, after), []).append(m)
            for (before, after), ms in groups.items():
                if any(m["mmr_after"] is not None for m in ms):
                    continue  # partly settled already: a combined change would count twice
                start = readings[before][1] if before is not None else None
                if after is None:
                    newest = datetime.fromisoformat(ms[-1]["ended_at"])
                    if self.now() - newest > STALE:
                        # An old session nobody read MMR for: don't lump it into the next one. The next
                        # reading sets the baseline, and matches after it get exact changes.
                        self.unknown_baseline.add(pid)
                        continue
                    self.waiting[pid] = Waiting(start, [(m["id"], datetime.fromisoformat(m["ended_at"])) for m in ms])
                    continue
                end = readings[after][1]
                if start is None or end == start:
                    continue  # can't tell what these matches did
                last = ms[-1]
                set_match_mmr(last["id"], start, end, covered=len(ms), path=self.db_path)
                updates.append({"playlist_id": pid, "before": start, "after": end, "delta": end - start,
                                "covered": len(ms), "source": "stored"})
        return updates

    def add_match(self, playlist_id: int, match_id: int, ended_at: datetime) -> None:
        if playlist_id not in TRACKED_PLAYLISTS:
            return
        w = self.waiting.get(playlist_id)
        if w is None:
            before = None if playlist_id in self.unknown_baseline else self.last_rating.get(playlist_id)
            w = self.waiting[playlist_id] = Waiting(before=before)
        w.matches.append((match_id, ended_at))

    def reading(self, playlist_id: int, value: int, source: str) -> list[dict]:
        """A saved reading. Returns the MMR updates it produced (empty when nothing was waiting).

        A reading equal to the rating before the waiting matches doesn't settle them: right after a
        match the game can still be showing the old number.
        """
        updates = []
        w = self.waiting.get(playlist_id)
        if w and w.matches and (w.before is None or value != w.before):
            for match_id, before, after, covered in settle(w, value):
                set_match_mmr(match_id, before, after, covered=covered, path=self.db_path)
                updates.append({"playlist_id": playlist_id, "before": before, "after": after,
                                "delta": None if before is None else after - before,
                                "covered": covered, "source": source})
            del self.waiting[playlist_id]
        if not (w and w.matches and value == w.before):
            self.last_rating[playlist_id] = value
            self.unknown_baseline.discard(playlist_id)
        return updates

    def expected(self, playlist_id: int) -> int | None:
        return self.last_rating.get(playlist_id)

    def waiting_count(self, playlist_id: int | None = None) -> int:
        if playlist_id is not None:
            w = self.waiting.get(playlist_id)
            return len(w.matches) if w else 0
        return sum(len(w.matches) for w in self.waiting.values())


# ====================================================================================================
# Epic Accounts: Optional helper: list Epic account IDs that the local Epic Games Launcher has stored.
#
# This only reads file names in the launcher's data folder. It is not a general lookup;
# other players are added by typing their player ID into the app.
# ====================================================================================================


DEFAULT_DATA_DIR = Path.home() / "AppData" / "Local" / "EpicGamesLauncher" / "Saved" / "Data"


def discover_local_account_ids(data_dir: Path | None = None) -> list[str]:
    base = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    if not base.is_dir():
        return []
    return sorted({f.stem for f in base.glob("*.dat") if len(f.stem) == 32})


def epic_player_id(account_id: str) -> str:
    return f"Epic|{account_id}|0"
