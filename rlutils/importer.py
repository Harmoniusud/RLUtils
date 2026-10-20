"""The one-time tracker.gg history import: check what the user pasted, before anything is saved.

The user opens a tracker.gg API page in their browser and pastes its JSON. Two kinds are accepted (both
shapes checked against the live API on 2026-10-20):

- the MMR history, api.tracker.gg/api/v1/rocket-league/player-history/mmr/<tracker.gg id>:
      {"data": {"<playlist id>": [{"rating", "tier", "division", "tierId", "divisionId", "collectDate"}, ...]}}
  It names no player, so it can't prove whose it is. It's checked against the readings already stored for
  the profile (same playlist and day): if most of them disagree, it's someone else's and is refused.
- the profile, api.tracker.gg/api/v2/rocket-league/standard/profile/epic/<name or account id>:
      {"data": {"platformInfo": {"platformUserId", "platformUserHandle", ...}, "metadata": {"playerId"}, ...}}
  It names the player: it must be the profile's Epic account (or, for a profile without one, its name). Its
  playerId is the tracker.gg id the history link needs.

No network access and no database writes here: the caller passes the stored readings in and saves the result.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .data import (
    SOURCE_TRACKER_IMPORT, Snapshot, TRACKED_PLAYLISTS, HistoryFormatError, parse_tracker_history, playlist_name,
)

HISTORY_URL = "https://api.tracker.gg/api/v1/rocket-league/player-history/mmr/{id}"
PROFILE_URL = "https://api.tracker.gg/api/v2/rocket-league/standard/profile/epic/{user}"
MAX_PASTE = 5_000_000      # characters; a real history is ~40 KB
RECENT_DAYS = 2            # tracker.gg's newest days still change during the day; not used to check identity
MIN_COMPARED = 3           # fewer overlapping days than this: can't tell whose it is
MIN_AGREEMENT = 0.5        # share of overlapping days that must agree


class PasteProblem(ValueError):
    """What's wrong with the paste, in words for the user."""


@dataclass
class ProfileCheck:
    """A pasted tracker.gg profile that belongs to the profile being imported into."""
    tracker_id: str
    handle: str


@dataclass
class PlaylistRow:
    playlist_id: int
    name: str
    readings: int
    new: int
    ranked: bool


@dataclass
class HistoryPreview:
    profile_key: str
    playlists: list[PlaylistRow]
    total: int                    # readings in the paste
    new: list[Snapshot]           # what an import adds
    stored: int                   # already stored, same value
    conflicts: int                # stored with another value for the same day: the stored one is kept
    malformed: int                # records that couldn't be read
    first: datetime
    last: datetime
    identity: str                 # how sure we are it's this player's, in words
    compared: int = 0
    agreed: int = 0
    examples: list[str] = field(default_factory=list)  # a few conflicts, for the user to look at


def history_url(tracker_id: str) -> str:
    return HISTORY_URL.format(id=tracker_id)


def profile_url(name: str) -> str:
    from urllib.parse import quote
    return PROFILE_URL.format(user=quote(name.strip(), safe=""))


def _account_hex(player_id: str | None) -> str | None:
    m = re.fullmatch(r"Epic\|([0-9a-fA-F]{32})\|\d+", (player_id or "").strip())
    return m.group(1).lower() if m else None


def parse_paste(text: str):
    """The pasted text as JSON, or PasteProblem saying what went wrong."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        raise PasteProblem("Nothing pasted yet. Copy the whole page from the tracker.gg tab (Ctrl+A, Ctrl+C) "
                           "and paste it into the box.")
    if len(text) > MAX_PASTE:
        raise PasteProblem("That's far more text than a tracker.gg history. Copy only the tracker.gg page.")
    low = text[:2000].lower()
    if low.startswith("<") or "<html" in low or "just a moment" in low or "cloudflare" in low:
        raise PasteProblem("That's a web page, not tracker.gg's data: probably its security check. Finish the "
                           "check in your browser until the page shows text starting with {\"data\", then copy "
                           "again.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        hint = (" It looks cut off: copy the whole page with Ctrl+A first." if exc.pos >= len(text) - 2
                else " In Firefox, click \"Raw Data\" before copying.")
        raise PasteProblem(f"That isn't valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}).{hint}") from None


def kind(payload) -> str:
    """'history', 'profile', 'error' (tracker.gg's own error answer) or 'unknown'."""
    if not isinstance(payload, dict):
        return "unknown"
    d = payload.get("data")
    if isinstance(d, dict) and isinstance(d.get("platformInfo"), dict):
        return "profile"
    if isinstance(d, dict) and d and all(re.fullmatch(r"\d+", str(k)) and isinstance(v, list) for k, v in d.items()):
        return "history"
    if isinstance(payload.get("errors"), list) and payload["errors"]:
        return "error"
    return "unknown"


def error_text(payload) -> str:
    first = payload["errors"][0]
    msg = first.get("message") if isinstance(first, dict) else str(first)
    return f"tracker.gg answered with an error: {msg or 'no details'}"


def check_profile(payload, profile_key: str, profile_player_id: str | None) -> ProfileCheck:
    """The pasted profile must be this profile's player: same Epic account id, or (for a profile with no
    account id, e.g. a tracked player) the same name. Raises PasteProblem otherwise."""
    d = payload["data"]
    info = d.get("platformInfo") or {}
    meta = d.get("metadata") or {}
    pid = meta.get("playerId")
    if isinstance(pid, bool) or not (isinstance(pid, int) or isinstance(pid, str) and pid.isdigit()):
        raise PasteProblem("That tracker.gg profile has no player id (data.metadata.playerId).")
    if str(info.get("platformSlug") or "epic").lower() != "epic":
        raise PasteProblem(f"That's a {info.get('platformSlug')} profile on tracker.gg; profiles here are Epic "
                           "accounts.")
    handle = str(info.get("platformUserHandle") or info.get("platformUserIdentifier") or "").strip()
    pasted_account = str(info.get("platformUserId") or "").replace("-", "").lower()
    mine = _account_hex(profile_player_id)
    if mine:
        if pasted_account != mine:
            raise PasteProblem(f"That tracker.gg profile is {handle or 'another player'}, not {profile_key}: its "
                               "Epic account id is different. Open the link shown here for this profile.")
    elif handle.lower() != profile_key.strip().lower():
        raise PasteProblem(f"That tracker.gg profile is {handle or 'another player'}, not {profile_key}.")
    return ProfileCheck(str(pid), handle or profile_key)


def _day(when: datetime) -> str:
    return when.date().isoformat()


def preview_history(payload, profile_key: str, stored: list[dict], identity_hint: str = "") -> HistoryPreview:
    """What importing this history into `profile_key` would do. `stored` is that profile's readings
    (data.all_snapshots). Raises PasteProblem if it isn't a usable history or belongs to someone else."""
    try:
        snaps, malformed = parse_tracker_history(payload)
    except HistoryFormatError as exc:
        raise PasteProblem(f"That JSON isn't a tracker.gg MMR history ({exc}).") from None
    if not snaps:
        raise PasteProblem("That history has no MMR readings in it"
                           + (f" ({malformed} records couldn't be read)." if malformed else "."))
    snaps = [Snapshot(s.playlist_id, s.rating, s.tier, s.division, s.tier_id, s.division_id, s.recorded_at,
                      SOURCE_TRACKER_IMPORT) for s in snaps]

    # Same playlist and moment, from an earlier import: same value = already stored; other value = conflict
    # (tracker.gg rewrote that day after the earlier import). The stored row is never replaced.
    imported: dict[tuple, set[int]] = {}
    by_day: dict[tuple, set[int]] = {}  # every stored reading of a day, any source, to check whose it is
    for r in stored:
        when = datetime.fromisoformat(r["recorded_at"])
        if r["source"] == SOURCE_TRACKER_IMPORT:
            imported.setdefault((r["playlist_id"], when), set()).add(r["rating"])
        by_day.setdefault((r["playlist_id"], _day(when)), set()).add(r["rating"])

    new: list[Snapshot] = []
    seen: set[tuple] = set()
    same = conflicts = 0
    examples: list[str] = []
    for s in snaps:
        k = (s.playlist_id, s.recorded_at, s.rating)
        if k in seen:  # the paste itself repeats a reading
            continue
        seen.add(k)
        have = imported.get((s.playlist_id, s.recorded_at))
        if have is None:
            new.append(s)
        elif s.rating in have:
            same += 1
        else:
            conflicts += 1
            if len(examples) < 3:
                examples.append(f"{playlist_name(s.playlist_id)} {s.recorded_at:%d %b %Y}: stored "
                                f"{'/'.join(map(str, sorted(have)))}, pasted {s.rating}")

    # Whose is it: compare with what's stored for the same playlist and day, leaving out tracker.gg's newest
    # days (they still change during the day).
    newest = max(s.recorded_at for s in snaps)
    compared = agreed = 0
    for s in {(s.playlist_id, _day(s.recorded_at)): s for s in snaps}.values():
        if s.recorded_at > newest - timedelta(days=RECENT_DAYS):
            continue
        have = by_day.get((s.playlist_id, _day(s.recorded_at)))
        if have:
            compared += 1
            agreed += any(abs(s.rating - v) <= 1 for v in have)  # +-1: other sources round differently
    if compared >= MIN_COMPARED and agreed / compared < MIN_AGREEMENT:
        raise PasteProblem(f"This history doesn't look like {profile_key}'s: on {compared - agreed} of {compared} "
                           "days where both have a reading, the MMR is different. Check that you opened the link "
                           "for this profile.")
    if compared >= MIN_COMPARED:
        identity = f"Matches {agreed} of {compared} days already stored for {profile_key}."
    elif identity_hint:
        identity = identity_hint
    else:
        identity = (f"Can't check whose it is: too little overlaps with what's stored for {profile_key}. "
                    "Only import it if it's this player's history.")

    rows = []
    for pid in sorted({s.playlist_id for s in snaps}, key=lambda p: (p not in TRACKED_PLAYLISTS, p)):
        rows.append(PlaylistRow(pid, playlist_name(pid), sum(s.playlist_id == pid for s in snaps),
                                sum(s.playlist_id == pid for s in new), pid in TRACKED_PLAYLISTS))
    return HistoryPreview(profile_key, rows, len(snaps), new, same, conflicts, malformed,
                          min(s.recorded_at for s in snaps), newest, identity, compared, agreed, examples)
