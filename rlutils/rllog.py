"""Ranked MMR from Rocket League's own log (Documents\\My Games\\Rocket League\\TAGame\\Logs).

Every time you start a queue the game writes, in order:

    Matchmaking: Pre-divide PartyLeaderMMR: 35.7555
    Matchmaking: StartMatchmaking at 2026-10-19 05:00:43 in USW3,USW1 for playlists 11 on game server ...

The number is the party leader's skill in that playlist; MMR as the game shows it = skill x 20 + 100, rounded
(checked on 2026-10-20 against Shift's OCR readings: 815, 827, 841, exact). The time is UTC. So a reading is
the MMR *before* that queue's match: the change from a match shows up when you queue the next time, and the
last match of a session only when you next queue. No reading is ever made up for a match that ended.

Whose MMR it is comes from the party lines around it:

    Party: HandleLocalPlayerLoginStatusChanged PlayerName=<name> PlayerID=Epic|<32 hex>|0 ... IsPrimary=True
    Party: OnPartyCreated ...                          you made a party: you lead it
    Party: HandleJoinLobby ... LobbyUID:'<id>'         you joined one (someone else's unless you just made it)
    Party: OnPartyLeaderChanged NewLeader=<id>
    Party: HandleLobbyMemberStatusUpdate LobbyUID=<id> Members=<n> MemberIndex=<i> MemberID=<id>
    Party: Back down to one player ... / Party Time out occurred ... / HandleLobbyDestroyed

A reading belongs to the signed-in player only when they queue alone or are the party's known leader; any
other reading is returned flagged (attributable=False, with the reason) and never saved.

Only those lines are looked at. The log's header holds the launcher's one-time exchange code on its
"Command line:" line; it never matches and nothing from it is kept. Reading is plain file I/O: the game
process is never touched.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RANKED = (10, 11, 13)  # Ranked Duel 1v1, Doubles 2v2, Standard 3v3
SELF = "self"          # leader marker: the signed-in player
STATE_VERSION = 1
CHUNK = 1 << 20

_PREFIX = re.compile(r"^\[[\d.:]+\]\s*")
_PLAYER = re.compile(r"^Party: HandleLocalPlayerLoginStatusChanged PlayerName=(?P<name>.*?) "
                     r"PlayerID=(?P<id>\S+) LoginStatus=LS_LoggedIn IsPrimary=True\b")
_CREATED = re.compile(r"^Party: OnPartyCreated bWasSuccessful=\(True\) InPartyId:'(?P<lobby>[^']*)'")
_JOINED = re.compile(r"^Party: HandleJoinLobby bWasSuccessful=\(True\) LobbyUID:'(?P<lobby>[^']*)'")
_LEADER = re.compile(r"^Party: OnPartyLeaderChanged NewLeader=(?P<id>\S+)")
_MEMBERS = re.compile(r"^Party: HandleLobbyMemberStatusUpdate LobbyUID=\S+ Members=(?P<n>\d+) "
                      r"MemberIndex=(?P<i>-?\d+) MemberID=(?P<id>\S*)")
_SOLO = re.compile(r"^Party: (Back down to one player|Party Time out occurred|HandleLobbyDestroyed)")
_SKILL = re.compile(r"^Matchmaking: Pre-divide PartyLeaderMMR: (?P<skill>-?\d+(?:\.\d+)?)\s*$")
_QUEUE = re.compile(r"^Matchmaking: StartMatchmaking at (?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) "
                    r"in \S* ?for playlists (?P<pl>[\d,]+)")


def skill_to_mmr(skill: float) -> int:
    """The MMR the game shows: skill x 20 + 100, rounded half up (35.7555 -> 815.11 -> 815)."""
    return math.floor(skill * 20 + 100 + 0.5)


def account_hex(player_id: str | None) -> str | None:
    """'Epic|<32 hex>|0' -> the 32-hex account id, lower case; None for other platforms or nothing."""
    m = re.fullmatch(r"Epic\|([0-9a-fA-F]{32})\|\d+", (player_id or "").strip())
    return m.group(1).lower() if m else None


@dataclass(frozen=True)
class Reading:
    playlist_id: int
    mmr: int
    skill: float
    queued_at: datetime         # aware, UTC
    account: str | None         # 32-hex Epic account id of the signed-in player
    name: str                   # their name in the game
    members: int                # party size when queueing (1 = alone)
    attributable: bool          # the MMR is the signed-in player's own
    reason: str = ""            # why not, when it isn't

    @property
    def key(self) -> tuple:
        return (self.playlist_id, self.queued_at.isoformat(), self.mmr)


class LogParser:
    """Feeds one log file's lines in order; returns a Reading for each ranked queue. Its state (who is signed
    in, the party, a skill line waiting for its queue line) survives being saved and restored, so a file can
    be read in pieces across restarts."""

    def __init__(self, state: dict | None = None):
        s = state or {}
        self.player_id: str | None = s.get("player_id")
        self.name: str = s.get("name", "")
        self.members: int = int(s.get("members", 1))
        self.leader: str | None = s.get("leader", SELF)  # SELF, another player's id, or None (unknown)
        self.created: str | None = s.get("created")      # lobby id of the party you made
        self.skill: float | None = s.get("skill")        # waiting for its StartMatchmaking line

    def state(self) -> dict:
        return {"player_id": self.player_id, "name": self.name, "members": self.members, "leader": self.leader,
                "created": self.created, "skill": self.skill}

    def _leader_is_me(self) -> bool:
        if self.leader == SELF:
            return True
        return bool(self.leader and self.player_id and self.leader.lower() == self.player_id.lower())

    def feed(self, line: str) -> Reading | None:
        if "Party: " not in line and "Matchmaking: " not in line:  # most lines; skip them cheaply
            return None
        line = _PREFIX.sub("", line.rstrip("\r\n"), count=1)
        if not (line.startswith("Party: ") or line.startswith("Matchmaking: ")):
            return None
        if m := _SKILL.match(line):
            self.skill = float(m["skill"])
        elif m := _QUEUE.match(line):
            return self._queue(m)
        elif m := _PLAYER.match(line):
            self.player_id, self.name = m["id"], m["name"].strip()
        elif m := _CREATED.match(line):
            self.created, self.leader, self.members = m["lobby"], SELF, 1
        elif m := _JOINED.match(line):
            if m["lobby"] != self.created:  # someone else's party: its leader comes in the lines that follow
                self.leader = None
        elif m := _LEADER.match(line):
            self.leader = m["id"]
        elif m := _MEMBERS.match(line):
            self.members = max(1, int(m["n"]))
            if self.members == 1:
                self.leader = SELF
            elif self.leader is None and m["i"] == "0" and m["id"]:
                self.leader = m["id"]  # the first member of a lobby you joined leads it
        elif _SOLO.match(line):
            self.members, self.leader = 1, SELF
        return None

    def _queue(self, m) -> Reading | None:
        skill, self.skill = self.skill, None
        playlists = [int(p) for p in m["pl"].split(",") if p]
        if skill is None or len(playlists) != 1 or playlists[0] not in RANKED:
            return None  # casual / extra modes, several playlists at once, or no skill line before it
        when = datetime.strptime(m["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        account = account_hex(self.player_id)
        if account is None:
            ok, why = False, "signed-in Epic account not found in the log yet"
        elif self.members > 1 and self._leader_is_me():
            ok, why = True, ""
        elif self.members > 1:
            ok, why = False, ("party led by someone else" if self.leader else "party leader unknown")
        else:
            ok, why = True, ""
        return Reading(playlists[0], skill_to_mmr(skill), skill, when, account, self.name, self.members, ok, why)


# ====================================================================================================
# Files: which logs exist, and reading each one from where the last read stopped.
# ====================================================================================================


def _known_documents() -> Path | None:
    """Documents as Windows has it: follows a move, e.g. to OneDrive (folder redirection / Known Folder Move)."""
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL = Documents
            return Path(buf.value)
    except (AttributeError, OSError):
        pass
    return None


def documents_candidates(env: dict | None = None, known: Path | None = None) -> list[Path]:
    """Where Documents can be, most likely first: Windows' own answer, then OneDrive's Documents (Known
    Folder Move, also when Windows still answers the old place), then the profile's Documents."""
    env = os.environ if env is None else env
    out: list[Path] = []
    for p in ([known] if known else []) + [Path(env[k]) / "Documents" for k in ("OneDrive", "OneDriveConsumer",
                                                                              "OneDriveCommercial") if env.get(k)] \
            + ([Path(env["USERPROFILE"]) / "Documents"] if env.get("USERPROFILE") else [Path.home() / "Documents"]):
        if p not in out:
            out.append(p)
    return out


def rocket_league_dir(candidates: list[Path] | None = None) -> Path:
    """Documents\\My Games\\Rocket League: the first candidate where it exists, else under Windows' Documents."""
    candidates = candidates if candidates is not None else documents_candidates(known=_known_documents())
    for docs in candidates:
        if (docs / "My Games" / "Rocket League").is_dir():
            return docs / "My Games" / "Rocket League"
    return candidates[0] / "My Games" / "Rocket League"


def default_log_dir() -> Path:
    """Documents\\My Games\\Rocket League\\TAGame\\Logs (see rocket_league_dir for where Documents is)."""
    return rocket_league_dir() / "TAGame" / "Logs"


def log_files(log_dir: Path) -> list[Path]:
    """Launch.log and its rotated copies (Launch-backup-*.log, Launch_2.log, ...), oldest first."""
    try:
        files = [p for p in log_dir.glob("Launch*.log") if p.is_file()]
    except OSError:
        return []
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(files, key=lambda p: (mtime(p), p.name))


def signed_in_players(log_dir: Path, files: int = 6, head: int = 4 << 20) -> list[tuple[str, str]]:
    """(32-hex Epic account id, in-game name) of the players signed into the game in the newest `files` logs,
    newest first, each once. The line comes early in a log, so only its first `head` bytes are read."""
    out: list[tuple[str, str]] = []
    for path in reversed(log_files(log_dir)[-files:]):
        try:
            with open(path, "rb") as f:
                text = f.read(head).decode("utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            if "IsPrimary=True" not in raw:
                continue
            m = _PLAYER.match(_PREFIX.sub("", raw, count=1))
            acc = account_hex(m["id"]) if m else None
            if acc and all(acc != a for a, _ in out):
                out.append((acc, m["name"].strip()))
    return out


def signature(path: Path) -> str | None:
    """A file's identity: its first line ("Log: Log file open, <date time>"), which stays the same when the
    game renames Launch.log to Launch-backup-<time>.log. None until that line is fully written."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    end = head.find(b"\n")
    if end <= 0:
        return None
    return head[:end].rstrip(b"\r").decode("utf-8", errors="replace")


class LogTailer:
    """Reads new complete lines from every log file and returns the ranked readings in them.

    State per file (by signature): the byte offset after the last complete line read, and the parser state at
    that point. A line still being written (the game writes in 4 KB blocks) is left for the next poll.
    poll() doesn't change the stored state; commit() does, so the caller can save the readings first: if it
    can't, the same lines are read again next time (saving is idempotent)."""

    def __init__(self, log_dir: Path, state: dict | None = None):
        self.log_dir = Path(log_dir)
        s = state if isinstance(state, dict) and state.get("version") == STATE_VERSION else {}
        self.files: dict[str, dict] = dict(s.get("files") or {})
        self._pending: dict[str, dict] | None = None

    def state(self) -> dict:
        return {"version": STATE_VERSION, "files": self.files}

    def poll(self) -> list[Reading]:
        files: dict[str, dict] = {}
        readings: list[Reading] = []
        for path in log_files(self.log_dir):
            sig = signature(path)
            if sig is None or sig in files:
                continue
            prev = self.files.get(sig) or {}
            offset = int(prev.get("offset", 0))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < offset:  # same first line but shorter: a different file; start over
                offset, prev = 0, {}
            parser = LogParser(prev.get("parser"))
            if size > offset:
                offset = self._read(path, offset, parser, readings)
            files[sig] = {"offset": offset, "parser": parser.state(), "name": path.name}
        self._pending = files
        return readings

    def commit(self) -> None:
        """Keep what the last poll read (files that are gone are forgotten)."""
        if self._pending is not None:
            self.files, self._pending = self._pending, None

    @staticmethod
    def _read(path: Path, offset: int, parser: LogParser, out: list[Reading]) -> int:
        try:
            f = open(path, "rb")
        except OSError:
            return offset
        with f:
            f.seek(offset)
            rest = b""
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                block = rest + chunk
                cut = block.rfind(b"\n")
                if cut < 0:
                    rest = block
                    continue
                for raw in block[:cut].split(b"\n"):
                    r = parser.feed(raw.decode("utf-8", errors="replace"))
                    if r is not None:
                        out.append(r)
                offset += cut + 1  # `block` starts at `offset`; everything up to the newline is read
                rest = block[cut + 1:]
            return offset


# ====================================================================================================
# Saving: readings into mmr_snapshots (source rl_log), through the usual database functions.
# ====================================================================================================


@dataclass
class SaveResult:
    saved: int = 0
    unchanged: int = 0                          # same MMR as the reading before it (a re-queue), or stored already
    skipped: dict[str, int] | None = None       # reason -> count, for readings that aren't the player's own
    last: tuple[str, Reading] | None = None     # (profile key, reading) of the newest one saved

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = {}


def save(readings: list[Reading], path: Path | None = None) -> SaveResult:
    """Store the player's own readings for the profile of their Epic account (added the first time, named as
    in the game). A reading equal to the previous rl_log reading of that playlist adds nothing: queueing
    again after a cancel shows the same MMR. Saving the same readings twice changes nothing."""
    from . import data  # here, so the parser above stays free of the database

    out = SaveResult()
    for r in sorted(readings, key=lambda r: r.queued_at):
        if not r.attributable:
            out.skipped[r.reason] = out.skipped.get(r.reason, 0) + 1
            continue
        key = data.profile_for_epic_account(r.account, r.name, path)
        prev = data.snapshot_before(key, r.playlist_id, r.queued_at, data.SOURCE_RL_LOG, path)
        if prev and prev["rating"] == r.mmr:
            out.unchanged += 1
            continue
        snap = data.Snapshot(r.playlist_id, r.mmr, "", "", None, None, r.queued_at, data.SOURCE_RL_LOG)
        if data.insert_snapshots(key, [snap], path):
            out.saved += 1
            out.last = (key, r)
        else:
            out.unchanged += 1
    return out


def load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)
