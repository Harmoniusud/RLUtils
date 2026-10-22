"""What the window shows, as plain data: the numbers behind the cards, lists and the chart of the web UI
(rlutils/web). Pure functions over rows from data.py; app.py sends the result to the page as JSON.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta

from . import stats
from .data import TIERS, aggregate, playlist_name, rank_for_mmr, rank_label

# Colours of the design (rlutils/web/index.html uses the same values).
TEXT, MUTED, FAINT = "#f3f3f3", "#a8a8a8", "#7d7d7d"
ACCENT, WIN, LOSS, WARN = "#B65CF2", "#6ccb5f", "#ff99a4", "#fce100"
_METAL_COLORS = ["#c9a27e", "#c4ccd6", "#e8c872", "#7fd6cf", "#8fb4dc", "#b9a2ff", "#ff8fa3"]
ROMAN = ["I", "II", "III", "IV"]

TITLES = {10: "Duel", 11: "Doubles", 13: "Standard", 61: "Quads"}
SHORT = {0: "Casual", 1: "Casual 1v1", 2: "Casual 2v2", 3: "Casual 3v3", 4: "Chaos", 10: "1v1", 11: "2v2", 13: "3v3",
         61: "4v4", 27: "Hoops", 28: "Rumble", 29: "Dropshot", 30: "Snowday", 34: "Tournament", 63: "Heatseeker"}


CASUAL = {0: "Cas", 1: "1v1", 2: "2v2", 3: "3v3", 4: "4v4"}  # the games list's mode column (casual noted in the row)


def title(pid: int) -> str:
    """'Doubles' for 2v2: the heading of the MMR page and the playlist of a game."""
    return TITLES.get(pid, playlist_name(pid))


def short(pid: int) -> str:
    """'2v2': segment buttons and the games list."""
    return SHORT.get(pid, playlist_name(pid))


def tier_color(tier_id: int | None) -> str:
    if tier_id is None:
        return "#b9a2ff"
    if tier_id <= 0:
        return MUTED
    if tier_id >= len(TIERS) - 1:
        return TEXT
    return _METAL_COLORS[(tier_id - 1) // 3]


def sign(n: float | None) -> str:
    """+9, −9 (a real minus), ±0."""
    if n is None:
        return ""
    n = int(round(n))
    return ("+" if n > 0 else "−" if n < 0 else "±") + str(abs(n))


def pct(w: int, n: int) -> str:
    return f"{round(w / n * 100)}%" if n else "—"


def short_date(dt: datetime) -> str:
    return f"{dt:%b} {dt.day}"


def when_label(dt: datetime, now: datetime) -> str:
    """'Today 21:48', 'Yesterday 20:10', 'Sat 19:30', 'Oct 2 18:05' (local times)."""
    days = (now.date() - dt.date()).days
    day = ("Today" if days <= 0 else "Yesterday" if days == 1 else f"{dt:%a}" if days < 7 else short_date(dt))
    return f"{day} {dt:%H:%M}"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def rank_text(tier_id: int | None, division_id: int | None) -> str:
    """'Diamond III · Div I' (Unranked and Supersonic Legend have no divisions)."""
    if tier_id is None or not 0 <= tier_id < len(TIERS):
        return rank_label(tier_id, division_id)
    if tier_id in (0, len(TIERS) - 1) or division_id is None:
        return TIERS[tier_id]
    return f"{TIERS[tier_id]} · Div {ROMAN[division_id] if 0 <= division_id < 4 else division_id + 1}"


def rank_of(row: dict, table: list | None) -> tuple[int | None, int | None]:
    """Stored tier/division, else looked up in the rank table."""
    if row.get("tier_id") is not None:
        return row["tier_id"], row.get("division_id")
    found = rank_for_mmr(table, row["rating"]) if table else None
    return found if found else (None, None)


# --- MMR page ----------------------------------------------------------------------------------------


def gauge(rating: int, tier: int | None, div: int | None, dist) -> dict:
    """The half-circle division gauge: progress through the current division and what's next."""
    if tier is None:
        return {"prog": 0, "pct": "—", "tier": "Rank unknown", "div": "", "toNext": "", "color": tier_color(None)}
    has_divs = 0 < tier < len(TIERS) - 1
    prog = None
    if dist and dist.to_next is not None and dist.to_drop is not None:
        prog = dist.to_drop / max(1, dist.to_drop + dist.to_next)
    to_next = ""
    if dist and dist.next_label and dist.to_next is not None:
        nxt = dist.next_label
        if nxt.startswith(TIERS[tier] + " Div "):
            nxt = nxt[len(TIERS[tier]) + 1:]
        elif " Div " in nxt:
            nxt = nxt.split(" Div ")[0]
        to_next = f"{'about ' if dist.estimated else ''}{dist.to_next} MMR to {nxt}"
    elif tier >= len(TIERS) - 1:
        to_next = "Top rank"
    return {
        "prog": max(0.0, min(1.0, prog)) if prog is not None else 0,
        "pct": f"{round(prog * 100)}%" if prog is not None else "—",
        "tier": TIERS[tier] if 0 <= tier < len(TIERS) else rank_label(tier, div),
        "div": f"Division {ROMAN[div or 0]}" if has_divs else "",
        "toNext": to_next,
        "color": tier_color(tier),
    }


def chart(rows: list[tuple[datetime, dict]], table: list | None, gran: str) -> dict:
    """Points, rank bands and division lines for the MMR chart. rows: (time, snapshot) in the range, oldest
    first. gran: 'raw', 'daily' or 'weekly'. X positions are by point index (as in the design); the page
    turns values into coordinates with lo/hi."""
    series = aggregate([(t, r["rating"]) for t, r in rows], gran)
    by_time = {t: r for t, r in rows}
    if not series:
        return {"points": [], "bands": [], "divs": [], "lo": 0, "hi": 1}
    ys = [v for _, v in series]
    lo = math.floor((min(ys) - 25) / 10) * 10
    hi = math.ceil((max(ys) + 25) / 10) * 10
    points = []
    for i, (t, v) in enumerate(series):
        row = by_time.get(t, {"rating": v})
        d = v - series[i - 1][1] if i else None
        tier, div = rank_of(row, table)
        if gran == "raw":
            sub = " · Win" if d and d > 0 else " · Loss" if d and d < 0 else ""
        else:
            sub = " · end of " + {"daily": "day", "weekly": "week"}.get(gran, "period")
        points.append({"v": v, "d": sign(d) if d is not None else "", "dColor": WIN if (d or 0) >= 0 else LOSS,
                       "date": short_date(t.astimezone()), "when": short_date(t.astimezone()) + sub,
                       "time": t.astimezone().strftime("%b %d, %Y %H:%M"), "rank": rank_text(tier, div) or "Rank unknown"})
    bands, divs = [], []
    if table:
        tiers: dict[int, list] = {}
        for tier, div, low, high in table:
            tiers.setdefault(tier, []).append((div, low, high))
        order = sorted(tiers, key=lambda t: min(b[1] for b in tiers[t]))
        starts = [min(b[1] for b in tiers[t]) for t in order]
        now = series[-1][1]
        # the rank the latest reading is in; only it and its neighbours get labels and division lines
        here = max((k for k, s in enumerate(starts) if s <= now), default=0)
        for k, tier in enumerate(order):
            tl = starts[k]
            th = starts[k + 1] if k + 1 < len(order) else max(b[2] for b in tiers[tier]) + 1
            a, b = max(tl, lo), min(th, hi)
            near = abs(k - here) <= 1
            if b > a:
                roomy = (b - a) / (hi - lo) >= 0.05  # a sliver of a band at the chart's edge: no label
                bands.append({"lo": a, "hi": b, "label": (TIERS[tier] if tier < len(TIERS) else str(tier))
                              if near and roomy else "", "color": tier_color(tier), "alt": tier % 2})
            if near:
                for div, low, _high in sorted(tiers[tier]):
                    if div > 0 and lo < low < hi:
                        divs.append({"v": low, "label": f"Div {ROMAN[div] if div < 4 else div + 1} · {low}"})
    return {"points": points, "bands": bands, "divs": divs, "lo": lo, "hi": hi}


def rank_table(table: list | None, rating: int | None, rank: tuple | None = None) -> list[dict]:
    """Every rank of a playlist, highest first, with each division's starting MMR and how far `rating` is from
    it (+ above it, - still to go). The division marked is `rank` (tier, division) when the reading came with
    one (the game's own rank), else the one `rating` falls in by the table."""
    if not table:
        return []
    here = tuple(rank) if rank and None not in rank else None
    if here is None and rating is not None:
        for tier, div, low, _high in table:
            if rating >= low:
                here = (tier, div)
    tiers: dict[int, list] = {}
    for tier, div, low, _high in table:
        tiers.setdefault(tier, []).append((div, low))
    rows = []
    for tier in sorted(tiers, reverse=True):
        divs = []
        for div, low in sorted(tiers[tier]):
            d = None if rating is None else rating - low
            divs.append({"label": "" if len(tiers[tier]) == 1 else f"Div {ROMAN[div] if div < 4 else div + 1}",
                         "low": low, "d": "" if d is None else ("±0" if d == 0 else sign(d)),
                         "dColor": MUTED if d is None else WIN if d >= 0 else LOSS, "here": here == (tier, div)})
        rows.append({"name": TIERS[tier] if tier < len(TIERS) else str(tier), "color": tier_color(tier),
                     "here": bool(here and here[0] == tier), "divs": divs})
    return rows


def mmr_cards(rows: list[tuple[datetime, dict]], before: int | None, table: list | None,
              games: list[dict]) -> list[dict]:
    """Net change, peak and win rate over the range. before: the last rating before the range (the start
    point of the net change), if any. games: saved games of this playlist in the range."""
    if not rows:
        return [{"label": "Net change", "value": "—", "color": TEXT, "sub": "No readings in range"},
                {"label": "Peak", "value": "—", "color": TEXT, "sub": ""},
                {"label": "Win rate", "value": pct(0, len(games)), "color": TEXT,
                 "sub": _wl(games) if games else "No games in range"}]
    ys = [r["rating"] for _, r in rows]
    start = before if before is not None else ys[0]
    net = ys[-1] - start
    peak_t, peak = max(rows, key=lambda tr: (tr[1]["rating"], tr[0]))
    peak_rank = rank_text(*rank_of(peak, table))
    if games:
        wins = sum(1 for g in games if g["won"])
        rate, sub = pct(wins, len(games)), _wl(games)
    else:  # no saved games: count the readings that went up as wins
        prev = [start] + ys[:-1]
        ups = sum(1 for p, y in zip(prev, ys) if y > p)
        downs = sum(1 for p, y in zip(prev, ys) if y < p)
        rate, sub = pct(ups, ups + downs), f"{ups}W – {downs}L from MMR changes"
    return [
        {"label": "Net change", "value": sign(net), "color": WIN if net >= 0 else LOSS,
         "sub": f"Over {len(rows)} reading{'s' if len(rows) != 1 else ''}"},
        {"label": "Peak", "value": str(peak["rating"]), "color": TEXT,
         "sub": " · ".join(x for x in (peak_rank, short_date(peak_t.astimezone())) if x)},
        {"label": "Win rate", "value": rate, "color": TEXT, "sub": sub},
    ]


def _wl(games: list[dict]) -> str:
    wins = sum(1 for g in games if g["won"])
    return f"{wins}W – {len(games) - wins}L"


# --- Games page --------------------------------------------------------------------------------------


def game_mmr_deltas(games: list[dict], snaps: dict[int, list[tuple[datetime, int]]],
                    max_wait: timedelta = timedelta(hours=2)) -> dict[int, int]:
    """MMR change per game, where the readings pin it down: the first reading after the game, minus the one
    before it, when this game is the only one of its playlist between the two. snaps: playlist -> (time,
    rating) oldest first. Games without a clear pair of readings are left out."""
    out: dict[int, int] = {}
    by_pl: dict[int, list] = {}
    for g in games:
        by_pl.setdefault(g["playlist_id"], []).append((_dt(g["ended_at"]), g["id"]))
    for pid, ends in by_pl.items():
        pts = snaps.get(pid) or []
        if len(pts) < 2:
            continue
        times = [t for t, _ in pts]
        ends.sort()
        end_times = [e for e, _ in ends]
        for end, gid in ends:
            j = bisect_left(times, end)
            if j == 0 or j >= len(pts) or pts[j][0] - end > max_wait:
                continue
            (t0, v0), (t1, v1) = pts[j - 1], pts[j]
            if bisect_right(end_times, t1) - bisect_right(end_times, t0) == 1:  # games ended in (t0, t1]
                out[gid] = v1 - v0
    return out


def session_positions(games: list[dict], gap: timedelta) -> dict[int, int]:
    """Game id -> its number in its session (1 = first). A gap over `gap` between games starts a session."""
    pos: dict[int, int] = {}
    prev_end, n = None, 0
    for g in sorted(games, key=lambda g: g["ended_at"]):
        start, end = _dt(g["started_at"] or g["ended_at"]), _dt(g["ended_at"])
        n = n + 1 if prev_end is not None and start - prev_end <= gap else 1
        pos[g["id"]] = n
        prev_end = end
    return pos


def current_session(games: list[dict], gap: timedelta, now: datetime) -> tuple[int, timedelta]:
    """(games, length) of the session going on now: the latest run of games, if its last one ended within
    `gap` of now (else (0, 0))."""
    ordered = sorted(games, key=lambda g: g["ended_at"], reverse=True)
    if not ordered or now - _dt(ordered[0]["ended_at"]) > gap:
        return 0, timedelta(0)
    run = [ordered[0]]
    for g in ordered[1:]:
        if _dt(run[-1]["started_at"] or run[-1]["ended_at"]) - _dt(g["ended_at"]) > gap:
            break
        run.append(g)
    return len(run), _dt(ordered[0]["ended_at"]) - _dt(run[-1]["started_at"] or run[-1]["ended_at"])


def _mvp(game: dict) -> dict | None:
    me = stats.me(game)
    if me is None:
        return None
    winners = [p for p in game["players"] if (p["team"] == me["team"]) == bool(game["won"])]
    return max(winners, key=lambda p: p["score"] or 0) if winners else None


def is_bot(p: dict) -> bool:
    """Bots (a bot fills in when a player leaves) have no account: the Stats API gives them Unknown|0|0."""
    return (p.get("primary_id") or "").lower().startswith("unknown")


def _mate_key(p: dict) -> str:
    key = (p.get("primary_id") or "").lower()
    return key or "name:" + p["name"]


RANKED_IDS = {10, 11, 13, 61, 27, 28, 29, 30, 34}  # ranked 1v1/2v2/3v3/4v4, ranked extra modes, tournaments
CASUAL_IDS = {0, 1, 2, 3, 4}
KINDS = {"All": None, "Ranked": RANKED_IDS, "Casual": CASUAL_IDS}


def mates_table(games: list[dict]) -> list[dict]:
    """Teammates by games played: name, games, wins."""
    seen: dict[str, dict] = {}
    for g in games:
        for p in g["players"]:
            if p["teammate"] and not is_bot(p):
                r = seen.setdefault(_mate_key(p), {"name": p["name"], "gp": 0, "w": 0})
                r["name"] = p["name"]
                r["gp"] += 1
                r["w"] += int(bool(g["won"]))
    return sorted(seen.values(), key=lambda r: (-r["gp"], -r["w"], r["name"].lower()))


def games_page(games: list[dict], pl: int | None, selected: int | None, deltas: dict[int, int],
               positions: dict[int, int], now: datetime, kind: str = "All") -> dict:
    """Everything on the Games page. games: all saved games of the profile, newest first. kind: "All",
    "Ranked" or "Casual"; pl: one playlist within it (None = all of them)."""
    if kind not in KINDS:
        kind = "All"
    ids = KINDS[kind]
    games = [g for g in games if ids is None or g["playlist_id"] in ids]
    pids = sorted({g["playlist_id"] for g in games})
    options = [{"id": None, "label": "All"}] + [{"id": p, "label": CASUAL.get(p, short(p)) if kind == "Casual"
                                                 else short(p)} for p in pids]
    if pl is not None and pl not in pids:
        pl = None
    gs = [g for g in games if pl is None or g["playlist_id"] == pl]
    sel = next((g for g in gs if g["id"] == selected), gs[0] if gs else None)

    wins = sum(1 for g in gs if g["won"])
    streak = 0
    while streak < len(gs) and bool(gs[streak]["won"]) == bool(gs[0]["won"]):
        streak += 1
    known = [deltas[g["id"]] for g in gs if g["id"] in deltas]
    rec = {"wl": f"{wins}–{len(gs) - wins}" if gs else "—", "pct": pct(wins, len(gs)) if gs else "",
           "pctColor": WIN if gs and wins / len(gs) >= 0.5 else LOSS,
           "streak": (("W" if gs[0]["won"] else "L") + str(streak)) if gs else "—",
           "streakColor": WIN if gs and gs[0]["won"] else LOSS,
           "mmr": f"{sign(sum(known))} MMR" if known else "MMR change unknown"}

    mine = [stats.me(g) for g in gs]
    mine = [m for m in mine if m]
    n = len(mine) or 1
    tot = {k: sum((m.get(k) or 0) for m in mine) for k in ("goals", "assists", "saves", "shots", "demos")}
    per = lambda k: f"{tot[k] / n:.2f}"  # noqa: E731
    style = stats.playstyle(gs)
    if style["tags"]:
        top = stats.tag_text(style["tags"])
    elif style["games"] >= stats.MIN_PROFILE_GAMES:
        top = "All-round"
    else:
        top = "—"
    style_card = {"top": top, "per": f"{per('goals')} G · {per('assists')} A · {per('saves')} Sv per game"
                  if mine else "No games yet",
                  "title": ("From your share of your team's stats" if style["games"] >= stats.MIN_PROFILE_GAMES
                            else f"Needs {stats.MIN_PROFILE_GAMES} games with your stats")}

    mlist = mates_table(gs)
    best = sorted((m for m in mlist if m["gp"] >= 2), key=lambda m: (-m["w"] / m["gp"], -m["gp"]))
    if best:
        b = best[0]
        mates = {"best": b["name"], "bestRec": f"{b['w']}–{b['gp'] - b['w']}",
                 "bestColor": WIN if b["w"] / b["gp"] >= 0.5 else LOSS,
                 "sub": f"Best duo of {len(mlist)} teammate{'s' if len(mlist) != 1 else ''} · "
                        f"{pct(b['w'], b['gp'])} win"}
    else:
        mates = {"best": "Solo" if gs else "—", "bestRec": "", "bestColor": WIN,
                 "sub": "No teammate with 2+ games in this filter" if gs else "No games yet"}

    glist = []
    for g in gs:
        won = bool(g["won"])
        tm = [p["name"] for p in g["players"] if p["teammate"] and not is_bot(p)]
        when = when_label(_dt(g["ended_at"]).astimezone(), now)
        d = deltas.get(g["id"])
        pid = g["playlist_id"]
        casual = pid in CASUAL
        glist.append({"id": g["id"], "res": "W" if won else "L", "win": won,
                      "pl": CASUAL.get(pid) or SHORT.get(pid, "")[:4] or str(pid), "plTitle": playlist_name(pid),
                      "score": f"{g['team_goals']} – {g['opp_goals']}",
                      "ot": "OT" if g["overtime"] else ("FF" if g["forfeit"] else ""),
                      "with": ("Casual · " if casual else "") + (("with " + ", ".join(tm)) if tm else "Solo"),
                      "when": when,
                      "delta": sign(d) if d is not None else "", "dColor": WIN if (d or 0) > 0 else LOSS,
                      "ctxTitle": f"{when} · {g['team_goals']}–{g['opp_goals']}"})

    detail = None
    if sel:
        won = bool(sel["won"])
        mvp = _mvp(sel)
        d = deltas.get(sel["id"])

        def row(p, you=False):
            return {"name": p["name"] + (" (bot)" if is_bot(p) else "") + (" (left)" if p.get("left_early") else ""),
                    "score": p["score"],
                    "g": p["goals"], "a": p["assists"], "sv": p["saves"], "sh": p["shots"],
                    "mvp": "MVP" if p is mvp else "", "you": you}

        me = stats.me(sel)
        groups = [{"label": "You", "color": "#d9adff", "rows": [row(p, True) for p in sel["players"] if p["is_self"]]},
                  {"label": "Teammates", "color": MUTED, "rows": [row(p) for p in sel["players"] if p["teammate"]]},
                  {"label": "Opponents", "color": LOSS,
                   "rows": [row(p) for p in sel["players"]
                            if not p["is_self"] and not p["teammate"] and (me is None or p["team"] != me["team"])]}]
        meta = [title(sel["playlist_id"]), when_label(_dt(sel["ended_at"]).astimezone(), now)]
        if sel["overtime"]:
            meta.append("Overtime")
        if sel["forfeit"]:
            meta.append("Forfeit")
        if sel.get("arena"):
            meta.append(sel["arena"])
        detail = {"id": sel["id"], "result": "WIN" if won else "LOSS", "win": won,
                  "score": f"{sel['team_goals']} – {sel['opp_goals']}", "meta": " · ".join(meta),
                  "delta": f"{sign(d)} MMR" if d is not None else "", "dColor": WIN if (d or 0) > 0 else LOSS,
                  "groups": [grp for grp in groups if grp["rows"]]}

    # "How you play": your averages per game
    shares = [{"value": per(k), "label": f"{label} per game", "color": c}
              for k, label, c in (("goals", "Goals", ACCENT), ("shots", "Shots", WARN), ("saves", "Saves", WIN),
                                  ("assists", "Assists", "#b9a2ff"), ("demos", "Demos", LOSS))]

    def pat(label, sel_games):
        w = sum(1 for g in sel_games if g["won"])
        return {"label": label, "n": f"{len(sel_games)} gp", "pct": pct(w, len(sel_games)),
                "w": f"{round(w / len(sel_games) * 100)}%" if sel_games else "0%",
                "color": WIN if sel_games and w / len(sel_games) >= 0.5 else LOSS}

    after = lambda v: [g for i, g in enumerate(gs) if i + 1 < len(gs) and bool(gs[i + 1]["won"]) == v]  # noqa: E731
    patterns = [pat("After a win", after(True)), pat("After a loss", after(False)),
                pat("Session games 1–4", [g for g in gs if positions.get(g["id"], 1) <= 4]),
                pat("Session games 5+", [g for g in gs if positions.get(g["id"], 1) > 4]),
                pat("You scored", [g for g in gs if (stats.me(g) or {}).get("goals")]),
                pat("Overtime", [g for g in gs if g["overtime"]])]
    found, note = stats.find_patterns(gs)
    standout = [{"text": p.text(), "color": WIN if p.kind == "win" else LOSS,
                 "n": f"{p.wins}W {p.losses}L, " + ("p<0.001" if p.p < 0.001 else f"p={p.p:.3f}")} for p in found]

    basis = f"Based on {len(gs)} game{'s' if len(gs) != 1 else ''}" + (f" in {title(pl)}" if pl is not None else f" ({kind.lower()})" if kind != "All" else "")
    return {
        "kinds": [{"id": k, "label": k} for k in KINDS], "kind": kind, "options": options, "pl": pl, "count": len(gs), "rec": rec, "style": style_card, "mates": mates,
        "list": glist, "sel": sel["id"] if sel else None, "detail": detail,
        "hyp": {"basis": basis, "shares": shares if mine else [],
                "mates": [{"name": m["name"], "gp": m["gp"], "wl": f"{m['w']}–{m['gp'] - m['w']}",
                           "pct": pct(m["w"], m["gp"]), "color": WIN if m["w"] / m["gp"] >= 0.5 else LOSS}
                          for m in mlist[:5]],
                "patterns": patterns if gs else [], "standout": standout, "note": note if gs else ""},
    }
