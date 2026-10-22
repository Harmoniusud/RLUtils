"""Per-game stats from Rocket League's own Stats API, and what they say about how you play.

The Stats API is Psyonix's official, local, read-only feed (https://www.rocketleague.com/developer/stats-api):
with PacketSendRate > 0 in TAStatsAPI.ini the game serves JSON on ws://127.0.0.1:49124 (WebPort) and
TCP 127.0.0.1:49123 (Port). RLUtils only listens: it never sends a command, and nothing touches the game.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ====================================================================================================
# Feed: Stats API messages -> one finished game. No sockets here, so it can be tested.
#
# Message: {"Event": name, "Data": {...}}; Data arrives as a JSON string inside the JSON (double encoded),
# on the WebSocket as well as on TCP. A game is followed from MatchCreated (or the first UpdateState) to
# MatchEnded, which carries WinnerTeamNum (-1 = no contest since v2.76). MatchDestroyed without MatchEnded
# (you left, or the game was cancelled) saves nothing.
# ====================================================================================================


# Stats API player fields -> our names.
PLAYER_FIELDS = {"Score": "score", "Goals": "goals", "Shots": "shots", "Assists": "assists", "Saves": "saves",
                 "Touches": "touches", "CarTouches": "car_touches", "Demos": "demos"}
STAT_KEYS = tuple(PLAYER_FIELDS.values()) + ("demoed",)
STAT_LABELS = {"score": "Score", "goals": "Goals", "shots": "Shots", "assists": "Assists", "saves": "Saves",
               "touches": "Touches", "car_touches": "Car touches", "demos": "Demos", "demoed": "Demoed",
               "boost_avg": "Avg boost"}


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def strip_split(primary_id) -> str:
    """'Epic|<hex>|0' -> 'epic|<hex>', so the splitscreen index and letter case don't matter."""
    if not isinstance(primary_id, str) or not primary_id:
        return ""
    parts = primary_id.split("|")
    return "|".join(parts[:2]).lower() if len(parts) >= 2 else primary_id.lower()


def decode(msg) -> tuple[str | None, dict]:
    """(event name, data dict) of one message; Data may be a dict or a JSON string."""
    if isinstance(msg, (bytes, str)):
        try:
            msg = json.loads(msg)
        except ValueError:
            return None, {}
    if not isinstance(msg, dict):
        return None, {}
    raw = msg.get("Data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except ValueError:
            raw = {}
    return msg.get("Event"), raw if isinstance(raw, dict) else {}


_EVENT = re.compile(r'"Event"\s*:\s*"(\w+)"')


def peek_event(msg) -> str | None:
    """The event name without decoding the message (it comes first in the envelope; else search it all)."""
    if isinstance(msg, dict):
        return msg.get("Event") if isinstance(msg.get("Event"), str) else None
    if isinstance(msg, bytes):
        msg = msg[:200].decode("utf-8", errors="replace") if b'"Event"' in msg[:200] else msg.decode("utf-8", "replace")
    if not isinstance(msg, str):
        return None
    m = _EVENT.search(msg, 0, 200) or _EVENT.search(msg)
    return m.group(1) if m else None


def decide_result(my_team: int, team_scores: dict, winner_team) -> bool | None:
    """Win (True) or loss (False) from the final score and your team. The score decides; WinnerTeamNum only
    when the score is level or unknown (a forfeit at a tie). None for a no-contest (-1) or nothing to go on.
    (The game has sent WinnerTeamNum for the losing side: a casual 2-0 win, goals 1+1 vs 0+0, on 20 Oct 2026.)"""
    if winner_team == -1:
        return None
    mine, theirs = team_scores.get(my_team), team_scores.get(1 - my_team)
    if mine is not None and theirs is not None and mine != theirs:
        return mine > theirs
    return winner_team == my_team if winner_team in (0, 1) else None


@dataclass
class GameRecord:
    match_guid: str
    playlist_id: int
    arena: str
    started_at: datetime
    ended_at: datetime
    my_team: int
    team_goals: int | None
    opp_goals: int | None
    won: bool
    overtime: bool
    forfeit: bool                 # ended with time left on the clock, outside overtime
    players: list[dict] = field(default_factory=list)  # see GameFeed._player_lines
    self_id: str = ""             # PrimaryId of you, as the game sent it
    self_name: str = ""
    self_how: str = ""            # how you were recognised: account, target, boost


class GameFeed:
    """Follows the current match. `known_ids` (stripped PrimaryIds of your accounts) is how you are found;
    the car the game shows you (Game.Target) and the one car with boost data are fallbacks. `on_game` gets a
    GameRecord when a match ends with a result; `log` gets a line for anything not saved."""

    # Events the feed uses; the rest (BallHit, BoostPickup, GoalScored, ...) are counted and dropped without
    # decoding their Data. UpdateState is decoded at most every STATE_EVERY_S; the newest one waits in
    # `pending` and is decoded before any other event, so a match always ends on its last state.
    USED = {"MatchCreated", "RoundStarted", "MatchInitialized", "UpdateState", "StatfeedEvent", "PlayerLeft",
            "ClockUpdatedSeconds", "MatchEnded", "MatchDestroyed"}
    STATE_EVERY_S = 0.2
    RATE_WINDOW_S = 10.0
    RATE_REPORT = 40       # messages a second (over RATE_WINDOW_S) worth a line in statsapi.log; normal is ~10-15

    def __init__(self, on_game, known_ids=frozenset(), log=None, save_offline: bool = False, on_rate=None,
                 clock=time.monotonic):
        self.on_game = on_game
        self.known_ids = frozenset(known_ids)
        self.log = log or (lambda *_: None)
        self.on_rate = on_rate or (lambda *_: None)
        self.save_offline = save_offline
        self.clock = clock
        self.live: dict = {}  # a small summary for the UI, replaced (never mutated) on each update
        self.pending = None
        self.last_state = -1e9
        self.counts: dict[str, int] = {}
        self.bytes = 0
        self.window_start = clock()
        self.reset()

    def reset(self) -> None:
        self.pending = None
        self.guid = ""
        self.playlist: int | None = None
        self.arena = ""
        self.started_at: datetime | None = None
        self.round_started = False
        self.team_scores: dict[int, int] = {}
        self.time_left: int | None = None
        self.overtime = False
        self.players: dict[str, dict] = {}   # key -> line
        self.target: tuple[str, int] | None = None
        self.done = False
        self.live = {}

    # --- input -----------------------------------------------------------------

    def handle(self, msg) -> None:
        """One message: a JSON string/bytes (WebSocket) or an already parsed envelope (TCP)."""
        event = peek_event(msg)
        now = self.clock()
        self._count(event, len(msg) if isinstance(msg, (str, bytes)) else 0, now)
        if event not in self.USED:
            return
        if event == "UpdateState" and now - self.last_state < self.STATE_EVERY_S:
            self.pending = msg  # newer state replaces an undecoded older one
            return
        if event == "UpdateState":
            self.pending = None  # superseded by this newer state
            self.last_state = now
        elif self.pending is not None:
            pending, self.pending = self.pending, None
            self._handle(*decode(pending))
        self._handle(*decode(msg))

    def _count(self, event, size: int, now: float) -> None:
        self.counts[event or "?"] = self.counts.get(event or "?", 0) + 1
        self.bytes += size
        span = now - self.window_start
        if span >= self.RATE_WINDOW_S:
            total = sum(self.counts.values())
            if total / span >= self.RATE_REPORT:
                top = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items(), key=lambda kv: -kv[1])[:8])
                self.on_rate(f"{total / span:.0f} msg/s, {self.bytes / span / 1024:.0f} KB/s over {span:.0f} s "
                             f"(playlist {self.playlist}, {len(self.players)} players, match ended: {self.done}): {top}")
            self.counts, self.bytes, self.window_start = {}, 0, now

    def _handle(self, event, data: dict) -> None:
        if not event:
            return
        if event == "MatchCreated":
            self.reset()
            self.guid = data.get("MatchGuid") or ""
            self.started_at = datetime.now(timezone.utc)
        elif event in ("RoundStarted", "MatchInitialized"):
            self.round_started = self.round_started or event == "RoundStarted"
        elif event == "UpdateState":
            self._update_state(data)
        elif event == "StatfeedEvent":
            self._statfeed(data)
        elif event == "PlayerLeft":
            key = strip_split(data.get("PrimaryId"))
            if key in self.players:
                self.players[key]["left_early"] = True
        elif event == "ClockUpdatedSeconds":
            if _int(data.get("TimeSeconds")) is not None:
                self.time_left = _int(data["TimeSeconds"])
            self.overtime = self.overtime or data.get("bOvertime") is True
        elif event == "MatchEnded":
            self._match_ended(data)
        elif event == "MatchDestroyed":
            if self.players and not self.done:
                self.log("Left before the match ended: not saved")
            self.reset()

    def _key(self, p: dict) -> str:
        key = strip_split(p.get("PrimaryId"))
        if not key or key.split("|")[-1] in ("", "0") or key.startswith("unknown"):  # bots share one id
            return f"name:{p.get('Name', '')}:{p.get('TeamNum')}"
        return key

    def _find(self, name, team) -> dict | None:
        for line in self.players.values():
            if line["name"] == name and line["team"] == team:
                return line
        return None

    def _update_state(self, data: dict) -> None:
        if self.done:
            return
        if not self.guid and isinstance(data.get("MatchGuid"), str):
            self.guid = data["MatchGuid"]
        if self.started_at is None:
            self.started_at = datetime.now(timezone.utc)
        game = data.get("Game") if isinstance(data.get("Game"), dict) else {}
        pid = _int(game.get("PlaylistId"))
        if pid is not None and pid >= 0:
            self.playlist = pid
        if isinstance(game.get("Arena"), str) and game["Arena"]:
            self.arena = game["Arena"]
        for t in game.get("Teams") or []:
            if isinstance(t, dict) and t.get("TeamNum") in (0, 1) and _int(t.get("Score")) is not None:
                self.team_scores[int(t["TeamNum"])] = _int(t["Score"])
        if _int(game.get("TimeSeconds")) is not None:
            self.time_left = _int(game["TimeSeconds"])
        self.overtime = self.overtime or game.get("bOvertime") is True
        target = game.get("Target")
        if game.get("bHasTarget") is True and isinstance(target, dict) and target.get("Name"):
            self.target = (target["Name"], target.get("TeamNum"))
        seen = set()
        for p in data.get("Players") or []:
            if not isinstance(p, dict) or p.get("TeamNum") not in (0, 1):
                continue
            key = self._key(p)
            seen.add(key)
            line = self.players.get(key)
            if line is None:
                line = self.players[key] = {"name": str(p.get("Name", "")), "primary_id": str(p.get("PrimaryId", "")),
                                            "team": int(p["TeamNum"]), "demoed": 0, "left_early": False,
                                            "_boost_sum": 0.0, "_boost_n": 0, "feed": {}}
            line["team"] = int(p["TeamNum"])
            line["left_early"] = False
            for src, dst in PLAYER_FIELDS.items():
                value = _int(p.get(src))
                if value is not None:
                    line[dst] = value
            boost = p.get("Boost")
            if isinstance(boost, (int, float)) and not isinstance(boost, bool) and not game.get("bReplay"):
                line["_boost_sum"] += float(boost)
                line["_boost_n"] += 1
            if line.get("touches") or line.get("score"):
                self.round_started = True  # joined mid-match: someone has played the ball, so it isn't cancelled
        for key, line in self.players.items():
            if key not in seen and data.get("Players"):
                line["left_early"] = True
        me_key, _how = self.find_self()
        self.live = {"playlist": self.playlist, "scores": dict(self.team_scores), "time_left": self.time_left,
                     "overtime": self.overtime, "players": len(seen), "online": bool(self.guid),
                     "my_team": self.players[me_key]["team"] if me_key else None}

    def _statfeed(self, data: dict) -> None:
        main, second = data.get("MainTarget"), data.get("SecondaryTarget")
        name = data.get("EventName") or data.get("Type")
        if isinstance(main, dict) and isinstance(name, str):
            line = self._find(main.get("Name"), main.get("TeamNum"))
            if line is not None:
                line["feed"][name] = line["feed"].get(name, 0) + 1
        if name == "Demolish" and isinstance(second, dict):
            victim = self._find(second.get("Name"), second.get("TeamNum"))
            if victim is not None:
                victim["demoed"] += 1

    # --- the end of a match ----------------------------------------------------

    def find_self(self) -> tuple[str | None, str]:
        """(player key, how) for you in this match, or (None, reason)."""
        for key in self.players:
            if key in self.known_ids:
                return key, "account"
        boosted = [k for k, line in self.players.items() if line["_boost_n"]]
        if {self.players[k]["team"] for k in boosted} == {0, 1}:
            return None, "spectating (cars on both teams send boost) and none of your accounts is in it"
        if self.target:
            for key, line in self.players.items():
                if (line["name"], line["team"]) == self.target:
                    return key, "target"
        if len(boosted) == 1:
            return boosted[0], "boost"
        return None, "none of your accounts is in this match"

    def _match_ended(self, data: dict) -> None:
        if self.done:
            return
        self.done = True
        winner = _int(data.get("WinnerTeamNum"))
        if not self.round_started:
            self.log("Match ended before kickoff: not saved (cancelled)")
            return
        if not self.guid and not self.save_offline:
            self.log("Offline match (no MatchGuid): not saved")
            return
        if self.playlist is None:
            self.log("Match ended before any state arrived: not saved")
            return
        me, how = self.find_self()
        if me is None:
            self.log(f"Match ended: not saved ({how})")
            return
        my_team = self.players[me]["team"]
        won = decide_result(my_team, self.team_scores, winner)
        if won is None:
            self.log(f"Match ended with winner {winner}: no result, not saved")
            return
        mine, theirs = self.team_scores.get(my_team), self.team_scores.get(1 - my_team)
        if winner in (0, 1) and mine is not None and theirs is not None and mine != theirs \
                and (winner == my_team) != (mine > theirs):
            self.on_rate(f"WinnerTeamNum {winner} disagrees with the score {mine}-{theirs} (you on {my_team}, "
                         f"playlist {self.playlist}); saved by the score")
        ended = datetime.now(timezone.utc)
        forfeit = bool(self.time_left and self.time_left > 0 and not self.overtime)
        self.on_game(GameRecord(
            match_guid=self.guid or f"local-{ended.isoformat()}", playlist_id=self.playlist, arena=self.arena,
            started_at=self.started_at or ended, ended_at=ended, my_team=my_team,
            team_goals=self.team_scores.get(my_team), opp_goals=self.team_scores.get(1 - my_team), won=won,
            overtime=self.overtime, forfeit=forfeit, players=self._player_lines(me),
            self_id=self.players[me]["primary_id"], self_name=self.players[me]["name"], self_how=how))

    def _player_lines(self, me: str) -> list[dict]:
        my_team = self.players[me]["team"]
        out = []
        for key, line in self.players.items():
            row = {k: v for k, v in line.items() if not k.startswith("_")}
            row["boost_avg"] = round(line["_boost_sum"] / line["_boost_n"], 1) if line["_boost_n"] else None
            row["is_self"] = key == me
            row["teammate"] = key != me and line["team"] == my_team
            for k in STAT_KEYS:
                row.setdefault(k, 0)
            out.append(row)
        # you first, then teammates, then opponents; best score first within each
        return sorted(out, key=lambda r: (not r["is_self"], r["team"] != my_team, -r["score"]))


# ====================================================================================================
# Reader: the TCP port (Port, 49123) with the WebSocket (WebPort, 49124) as a fallback. Reconnects until
# stopped. Read-only: nothing is ever sent to the game.
#
# TCP first since 20 Oct 2026: with RLUtils on the WebSocket, Rocket League's FPS dropped hard in an Honor
# Duel (the 1v1 played on inside a finished match) and recovered when RLUtils quit, while Shift sat on the
# TCP port the whole time without trouble. Whatever the game does on its WebSocket side then, TCP is the
# path known to be harmless.
# ====================================================================================================


MAX_PENDING_BYTES = 1_000_000  # drop a TCP connection that never produces decodable JSON


def _safe_handle(feed: GameFeed, msg, events: queue.Queue) -> None:
    """A message the feed chokes on is reported, not fatal: the reader must keep draining the socket."""
    try:
        feed.handle(msg)
    except Exception as exc:  # noqa: BLE001 (unknown game data)
        feed.reset()
        events.put(("error", f"{type(exc).__name__}: {exc} in {str(msg)[:300]}"))


def _read_ws(feed: GameFeed, host: str, port: int, stop: threading.Event, events: queue.Queue) -> bool:
    """Read the WebSocket until it closes. False if it couldn't connect."""
    import websocket  # websocket-client
    try:
        ws = websocket.create_connection(f"ws://{host}:{port}", timeout=3)
    except (OSError, websocket.WebSocketException):
        return False
    events.put(("status", f"connected (WebSocket {port})"))
    ws.settimeout(1.0)
    try:
        while not stop.is_set():
            try:
                _op, raw = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                continue
            if raw is None or raw == b"":
                if not ws.connected:
                    break
                continue
            _safe_handle(feed, raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw, events)
    except (OSError, websocket.WebSocketException):
        pass
    finally:
        ws.close()
    return True


def _read_tcp(feed: GameFeed, host: str, port: int, stop: threading.Event, events: queue.Queue) -> bool:
    """Read the TCP stream (JSON objects back to back) until it closes. False if it couldn't connect."""
    try:
        sock = socket.create_connection((host, port), timeout=3)
    except OSError:
        return False
    events.put(("status", f"connected (TCP {port})"))
    sock.settimeout(1.0)
    decoder, buf = json.JSONDecoder(), ""
    try:
        while not stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            feed.bytes += len(chunk)
            buf += chunk.decode("utf-8", errors="replace")
            pos, end = 0, len(buf)
            while True:
                while pos < end and buf[pos] in " \t\r\n":
                    pos += 1
                if pos >= end:
                    break
                try:
                    obj, pos2 = decoder.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    break  # the rest of this object comes with the next chunk
                pos = pos2
                _safe_handle(feed, obj, events)
            buf = buf[pos:]
            if len(buf) > MAX_PENDING_BYTES:
                break
    except OSError:
        pass
    finally:
        sock.close()
    return True


def run_reader(feed: GameFeed, host: str, web_port: int, tcp_port: int, stop: threading.Event,
               events: queue.Queue, retry_s: float = 5.0, is_game_running=None) -> None:
    """Connect, read, reconnect, until `stop` is set. Puts ("status", text) and ("off", text) on `events`."""
    while not stop.is_set():
        connected = bool(tcp_port) and _read_tcp(feed, host, tcp_port, stop, events)
        if not connected and web_port:
            connected = _read_ws(feed, host, web_port, stop, events)
        if connected:
            feed.reset()  # a match cut off by the disconnect can't be finished
            events.put(("status", "waiting"))
            stop.wait(1.0)
            continue
        if is_game_running is not None and is_game_running():
            events.put(("off", "Rocket League is running but its Stats API isn't answering"))
        else:
            events.put(("status", "waiting"))
        stop.wait(retry_s)


# ====================================================================================================
# Analysis: playstyle tags, teammates and win/loss patterns. Pure functions over saved games
# (data.list_games rows: game fields plus "players", each with is_self / teammate / team and the stats).
# ====================================================================================================


STYLES = {  # tag -> (stat, smallest team total that counts, label)
    "shooter": ("shots", 3, "Shooter"),
    "keeper": ("saves", 2, "Keeper"),
    "playmaker": ("assists", 2, "Playmaker"),
}
STYLE_LABELS = {**{k: v[2] for k, v in STYLES.items()}, "demo": "Demo-heavy"}
SHARE_FACTOR = 1.5      # a game's tag: at least 1.5x a fair share of the stat
PROFILE_FACTOR = 1.25   # your overall style: on average at least 1.25x a fair share
MIN_PROFILE_GAMES = 5
MIN_SIDE = 5            # patterns need at least this many wins and this many losses
MIN_COVER = 0.6         # a pattern holds in at least 60% of the wins (or losses) ...
MIN_GAP = 0.3           # ... and in at least 30 points fewer of the other side
MAX_P = 0.05            # Fisher's exact test, two-sided


def me(game: dict) -> dict | None:
    return next((p for p in game["players"] if p["is_self"]), None)


def side(game: dict, player: dict, same: bool) -> list[dict]:
    """The player's team (same=True, including them) or the other team."""
    return [p for p in game["players"] if (p["team"] == player["team"]) == same]


def share(game: dict, player: dict, stat: str) -> tuple[float | None, float, int]:
    """(share, fair share, total the share is of). In a team: of the team's total. Alone on a team
    (1v1): of both players' total, so the fair share is a half."""
    team = side(game, player, True)
    pool = team if len(team) > 1 else game["players"]
    total = sum(p.get(stat) or 0 for p in pool)
    fair = 1 / len(team) if len(team) > 1 else 1 / max(1, len(pool))
    return ((player.get(stat) or 0) / total if total else None), fair, total


def game_tags(game: dict, player: dict) -> list[str]:
    tags = []
    for tag, (stat, min_total, _label) in STYLES.items():
        s, fair, total = share(game, player, stat)
        if s is not None and total >= min_total and s >= fair * SHARE_FACTOR:
            tags.append(tag)
    others = [p.get("demos") or 0 for p in game["players"] if p is not player]
    mean = sum(others) / len(others) if others else 0
    if (player.get("demos") or 0) >= 2 and (player.get("demos") or 0) >= 2 * mean:
        tags.append("demo")
    return tags


def tag_text(tags: list[str]) -> str:
    return ", ".join(STYLE_LABELS[t] for t in tags)


def playstyle(games: list[dict]) -> dict:
    """Your average share of each style stat against a fair share, how often each tag fits, and the styles
    that fit overall: {"games", "styles": [(tag, avg share, fair, tagged games)], "tags": [tag]}."""
    games = [g for g in games if me(g)]
    out = {"games": len(games), "styles": [], "tags": []}
    for tag, (stat, _min_total, _label) in STYLES.items():
        rows = [share(g, me(g), stat) for g in games]
        rows = [(s, f) for s, f, _t in rows if s is not None]
        tagged = sum(1 for g in games if tag in game_tags(g, me(g)))
        if not rows:
            continue
        avg = sum(s for s, _ in rows) / len(rows)
        fair = sum(f for _, f in rows) / len(rows)
        out["styles"].append((tag, avg, fair, tagged))
        if len(games) >= MIN_PROFILE_GAMES and avg >= fair * PROFILE_FACTOR:
            out["tags"].append(tag)
    demo_games = sum(1 for g in games if "demo" in game_tags(g, me(g)))
    mine = [me(g).get("demos") or 0 for g in games]
    lobby = [p.get("demos") or 0 for g in games for p in g["players"] if not p["is_self"]]
    if games:
        out["demos"] = (sum(mine) / len(mine), sum(lobby) / len(lobby) if lobby else 0.0, demo_games)
        if len(games) >= MIN_PROFILE_GAMES and out["demos"][0] >= 1 and out["demos"][0] >= 1.5 * out["demos"][1]:
            out["tags"].append("demo")
    return out


def teammates(games: list[dict], min_games: int = 3) -> dict:
    """How your teammates played: average score next to yours in wins and in losses, how often each tag fit
    them, and the teammates you played with at least `min_games` times (by account)."""
    team_games = [g for g in games if me(g) and any(p["teammate"] for p in g["players"])]
    out = {"games": len(team_games), "score": {}, "tags": {}, "regulars": []}
    for label, pick in (("wins", True), ("losses", False)):
        sel = [g for g in team_games if bool(g["won"]) == pick]
        mates = [p["score"] for g in sel for p in g["players"] if p["teammate"]]
        mine = [me(g)["score"] for g in sel]
        if sel:
            out["score"][label] = (sum(mine) / len(mine), sum(mates) / len(mates), len(sel))
    lines = 0
    for g in team_games:
        for p in g["players"]:
            if p["teammate"]:
                lines += 1
                for t in game_tags(g, p):
                    out["tags"][t] = out["tags"].get(t, 0) + 1
    out["lines"] = lines
    seen: dict[str, dict] = {}
    for g in team_games:
        for p in g["players"]:
            key = (p.get("primary_id") or "").lower()
            if p["teammate"] and key and not key.startswith("unknown"):
                r = seen.setdefault(key, {"name": p["name"], "games": 0, "wins": 0, "score": 0})
                r["name"] = p["name"]
                r["games"] += 1
                r["wins"] += int(bool(g["won"]))
                r["score"] += p["score"]
    out["regulars"] = sorted((r for r in seen.values() if r["games"] >= min_games), key=lambda r: -r["games"])
    return out


# Values the pattern finder looks at: (who, stat). "you" = you; "mates" = your teammates together;
# "opps" = your opponents together. Opponents' goals are left out (they are the result itself).
PATTERN_METRICS = [("you", s) for s in ("goals", "shots", "saves", "assists", "demos", "touches", "score")] + \
                  [("mates", s) for s in ("goals", "shots", "saves", "assists", "demos", "score")] + \
                  [("opps", s) for s in ("shots", "saves", "demos", "score")]
WHO = {"you": "you have", "mates": "your teammates have", "opps": "your opponents have"}


def metric(game: dict, who: str, stat: str) -> int | None:
    self_ = me(game)
    if self_ is None:
        return None
    if who == "you":
        return self_.get(stat) or 0
    rows = [p for p in game["players"] if (p["teammate"] if who == "mates" else p["team"] != self_["team"])]
    return sum(p.get(stat) or 0 for p in rows) if rows else None


def fisher_p(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher's exact test of the 2x2 table [[a, b], [c, d]]."""
    n, row1, col1 = a + b + c + d, a + b, a + c

    def prob(x):
        return math.comb(col1, x) * math.comb(n - col1, row1 - x) / math.comb(n, row1)

    observed = prob(a)
    lo, hi = max(0, row1 + col1 - n), min(row1, col1)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * (1 + 1e-9)))


@dataclass
class Pattern:
    kind: str          # "win": holds in most wins, "loss": holds in most losses
    who: str
    stat: str
    over: bool         # True: value > threshold; False: value <= threshold
    threshold: int
    in_wins: float     # share of wins where it holds
    in_losses: float
    wins: int
    losses: int
    p: float

    def text(self) -> str:
        one, many = UNITS[self.stat]
        t = self.threshold
        if self.over:
            cond = f"at least one {one}" if t == 0 else f"over {t} {many}"
        else:
            cond = f"no {many}" if t == 0 else f"{t} or fewer {one if t == 1 else many}"
        together = " together" if self.who != "you" else ""
        if self.kind == "win":
            return f"{self.in_wins:.0%} of wins {WHO[self.who]} {cond}{together} (vs {self.in_losses:.0%} of losses)"
        return f"{self.in_losses:.0%} of losses {WHO[self.who]} {cond}{together} (vs {self.in_wins:.0%} of wins)"


UNITS = {"score": ("point", "points"), "goals": ("goal", "goals"), "shots": ("shot", "shots"),
         "saves": ("save", "saves"), "assists": ("assist", "assists"), "demos": ("demo", "demos"),
         "touches": ("touch", "touches")}


def _rank(p: Pattern) -> tuple:
    """Smaller is better: lowest p, then the widest gap, then the strongest statement that is still true
    (the highest 'over' threshold, the lowest 'or fewer' one)."""
    return p.p, -abs(p.in_wins - p.in_losses), -p.threshold if p.over else p.threshold


def find_patterns(games: list[dict], limit: int = 8) -> tuple[list[Pattern], str]:
    """Thresholds where wins and losses differ the most, per value; only ones that hold in at least MIN_COVER
    of one side, MIN_GAP more than the other, with Fisher p <= MAX_P, and with MIN_SIDE wins and losses
    behind them. Returns (patterns, note); the note says why there are none."""
    games = [g for g in games if me(g)]
    wins = [g for g in games if g["won"]]
    losses = [g for g in games if not g["won"]]
    if len(wins) < MIN_SIDE or len(losses) < MIN_SIDE:
        return [], (f"Patterns need at least {MIN_SIDE} wins and {MIN_SIDE} losses with stats "
                    f"(now {len(wins)}W {len(losses)}L).")
    found = []
    for who, stat in PATTERN_METRICS:
        wv = [v for v in (metric(g, who, stat) for g in wins) if v is not None]
        lv = [v for v in (metric(g, who, stat) for g in losses) if v is not None]
        if len(wv) < MIN_SIDE or len(lv) < MIN_SIDE:
            continue
        values = sorted(set(wv + lv))
        cuts = values[:-1]  # "over the largest value" never holds
        if stat == "score":  # points: round numbers read better than "over 422"
            cuts = sorted({v // 50 * 50 for v in cuts if v // 50 * 50 < values[-1]})
        best: dict[str, Pattern] = {}
        for t in cuts:
            for over in (True, False):
                a = sum(1 for v in wv if (v > t) == over)
                c = sum(1 for v in lv if (v > t) == over)
                pw, pl = a / len(wv), c / len(lv)
                for kind, hit, miss in (("win", pw, pl), ("loss", pl, pw)):
                    if hit < MIN_COVER or hit - miss < MIN_GAP:
                        continue
                    p = fisher_p(a, len(wv) - a, c, len(lv) - c)
                    if p > MAX_P:
                        continue
                    cand = Pattern(kind, who, stat, over, t, pw, pl, len(wv), len(lv), p)
                    old = best.get(kind)
                    if old is None or _rank(cand) < _rank(old):
                        best[kind] = cand
        pair = list(best.values())
        if len(pair) == 2 and pair[0].threshold == pair[1].threshold:
            # "most wins: over 3 shots" and "most losses: 3 or fewer" are one split said twice; keep the "over"
            # one unless the other covers clearly more of its side
            pair = [max(pair, key=lambda x: (x.in_wins if x.kind == "win" else x.in_losses) + (0.15 if x.over else 0))]
        found.extend(pair)
    found.sort(key=_rank)
    if not found:
        return [], f"No clear difference between your wins and losses yet ({len(wins)}W {len(losses)}L)."
    return found[:limit], ""


# ====================================================================================================
# Setting: TAStatsAPI.ini, where the Stats API is switched on (Documents\My Games\Rocket League\TAGame\Config).
#
#   [TAGame.MatchStatsExporter_TA]
#   Port=49123
#   PacketSendRate=10        0 (or missing) = off; updates per second, up to 120
#
# The game reads it when it starts. Only enable_stats_api writes it, and only when the user asks (setup or
# Settings); everything else in the file is left as it is.
# ====================================================================================================

STATS_SECTION = "TAGame.MatchStatsExporter_TA"
DEFAULT_RATE = 10
DEFAULT_PORT = 49123


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


def stats_ini_path(rl_dir: Path | None = None) -> Path:
    """The player's copy: Documents\\My Games\\Rocket League\\TAGame\\Config\\TAStatsAPI.ini. The game makes it
    from the install folder's file (below) and remakes it when that one changes (its [IniVersion] stamp)."""
    return Path(rl_dir or rocket_league_dir()) / "TAGame" / "Config" / "TAStatsAPI.ini"


def install_ini_path(install_dir: Path) -> Path:
    """The file Psyonix says to edit: <install>\\TAGame\\Config\\TAStatsAPI.ini, or DefaultStatsAPI.ini if that
    doesn't exist. Seen on 2026-10-20: switching it on there (Shift did, on 7 Oct) made the game rewrite the
    Documents copy 39 s later, so only editing the Documents copy can be undone by a game update."""
    config = Path(install_dir) / "TAGame" / "Config"
    user_named = config / "TAStatsAPI.ini"
    return user_named if user_named.exists() else config / "DefaultStatsAPI.ini"


def stats_api_state(user_ini: Path, install_ini: Path | None = None) -> dict:
    """Whether the Stats API is on, as the game will see it: the Documents copy if it exists, else the install
    folder's file. `install_on` says whether the install folder's file is on too (None: not known)."""
    user = read_stats_api(user_ini)
    inst = read_stats_api(install_ini) if install_ini else None
    state = dict(user if user["exists"] or not (inst and inst["exists"]) else inst)
    state["install_on"] = inst["enabled"] if inst and inst["exists"] else None
    state["install_path"] = str(install_ini) if install_ini else ""
    return state


def enable_stats_everywhere(user_ini: Path, install_ini: Path | None = None) -> tuple[list[str], list[str]]:
    """Switch it on in the install folder's file (so a game update or a rewrite of the Documents copy keeps it
    on) and in the Documents copy (what the game reads now). Returns (files changed, problems): a file that
    can't be written (e.g. an install under Program Files without write access) is a problem, not a crash."""
    changed, problems = [], []
    targets = ([install_ini] if install_ini and install_ini.parent.is_dir() else []) + [user_ini]
    for path in targets:
        if read_stats_api(path)["enabled"]:
            continue
        try:
            enable_stats_api(path)
            changed.append(str(path))
        except OSError as exc:
            problems.append(f"{path}: {exc.strerror or exc}")
    return changed, problems


def _read_ini_text(path) -> tuple[str, str]:
    """(text, encoding) of an ini file: UTF-16 or UTF-8 (BOM kept), else plain UTF-8."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _section_values(text: str, section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            inside = s[1:-1].strip().lower() == section.lower()
            continue
        if inside and "=" in s and not s.startswith((";", "#")):
            key, value = s.split("=", 1)
            values.setdefault(key.strip().lower(), value.strip())
    return values


def read_stats_api(path) -> dict:
    """{"exists", "enabled", "rate", "port", "path"} from TAStatsAPI.ini."""
    out = {"exists": False, "enabled": False, "rate": 0, "port": DEFAULT_PORT, "path": str(path)}
    try:
        text, _enc = _read_ini_text(path)
    except OSError:
        return out
    values = _section_values(text, STATS_SECTION)
    out["exists"] = True
    try:
        out["rate"] = int(float(values.get("packetsendrate", "0") or 0))
    except ValueError:
        out["rate"] = 0
    try:
        out["port"] = int(values.get("port", DEFAULT_PORT))
    except ValueError:
        pass
    out["enabled"] = out["rate"] > 0
    return out


def enable_stats_api(path, rate: int = DEFAULT_RATE) -> str:
    """Switch the Stats API on: PacketSendRate=`rate` (and Port, if missing) in the [TAGame.MatchStatsExporter_TA]
    section, which is added if needed. Other lines, the encoding and line endings stay as they were; the old file
    is kept as TAStatsAPI.ini.bak. Returns what it did. The caller makes sure the game isn't running."""
    path = Path(path)
    if path.exists():
        text, enc = _read_ini_text(path)
    else:
        text, enc = "", "utf-8"
    nl = "\r\n" if "\r\n" in text or not text else "\n"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.strip().lower() == f"[{STATS_SECTION}]".lower()), None)
    if start is None:
        block = [f"[{STATS_SECTION}]", f"Port={DEFAULT_PORT}", f"PacketSendRate={rate}"]
        lines = block + ([""] + lines if lines else [])
        did = "added the Stats API section"
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        keys = {l.split("=", 1)[0].strip().lower(): i for i, l in enumerate(lines[start + 1:end], start + 1) if "=" in l}
        if "packetsendrate" in keys:
            lines[keys["packetsendrate"]] = f"PacketSendRate={rate}"
        else:
            lines.insert(end, f"PacketSendRate={rate}")
            end += 1
        if "port" not in keys:
            lines.insert(start + 1, f"Port={DEFAULT_PORT}")
        did = f"set PacketSendRate={rate}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
    path.write_text(nl.join(lines) + nl, encoding=enc, newline="")
    return did
