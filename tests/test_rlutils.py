"""All automatic checks: python -m unittest tests.test_rlutils (offline, synthetic data)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rlutils import app, data, viewmodel
from rlutils.data import (
    HistoryFormatError, SOURCE_CURRENT, SOURCE_TRACKER_IMPORT, Snapshot, aggregate, current_streak,
    longest_streaks, parse_timestamp, parse_tracker_history, rank_changes, rank_label, split_sessions,
    summarize, tier_name,
)


# ====================================================================================================
# From test_backend_extras.py
# ====================================================================================================
ME = "Epic|0123456789abcdef0123456789abcdef|0"


def m(hour, won, **stats):
    return {"ended_at": datetime(2026, 10, 12, hour, 0, tzinfo=timezone.utc).isoformat(), "won": int(won),
            "playlist_id": 11, "mmr_delta": stats.pop("delta", None), **stats}


class InsightTests(unittest.TestCase):
    def test_averages_skip_missing(self):
        rows = [m(1, True, goals=2, saves=1), m(2, False, goals=0), m(3, True)]
        avg = data.stat_averages(rows)
        self.assertEqual(avg["goals"], 1.0)
        self.assertEqual(avg["saves"], 1.0)
        self.assertIsNone(avg["demos"])
        self.assertEqual(len(data.with_stats(rows)), 2)

    def test_wins_vs_losses(self):
        rows = [m(1, True, goals=3), m(2, True, goals=1), m(3, False, goals=0)]
        self.assertEqual(data.wins_vs_losses(rows)["goals"], (2.0, 0.0))

    def test_heatmap_and_best_hours(self):
        rows = [m(20, True)] * 4 + [m(20, False)] + [m(9, False)] * 5
        grid = data.heatmap(rows, tz=timezone.utc)
        self.assertEqual(grid[0][20], (4, 5))  # 12 Oct 2026 is a Monday
        best = data.best_hours(grid, min_games=5)
        self.assertEqual(best[0][:2], (20, 0.8))

# ====================================================================================================
# From test_history.py
# ====================================================================================================
FIXTURE = Path(__file__).parent / "fixtures" / "tracker_history_sample.json"  # synthetic (made-up values)


class ParseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_parses_playlist_keys_and_counts(self):
        snaps, skipped = parse_tracker_history(self.payload)
        by_playlist = {}
        for s in snaps:
            by_playlist[s.playlist_id] = by_playlist.get(s.playlist_id, 0) + 1
        self.assertEqual(by_playlist, {0: 1, 11: 14, 13: 1})
        self.assertEqual(skipped, 2)  # bad date and missing rating in playlist 13

    def test_sorted_chronologically_per_playlist(self):
        snaps, _ = parse_tracker_history(self.payload)
        doubles = [s for s in snaps if s.playlist_id == 11]
        self.assertEqual(doubles[0].rating, 462)
        self.assertEqual(doubles[-1].rating, 1036)
        self.assertTrue(all(a.recorded_at <= b.recorded_at for a, b in zip(doubles, doubles[1:])))

    def test_timestamps_are_aware_utc(self):
        snaps, _ = parse_tracker_history(self.payload)
        self.assertTrue(all(s.recorded_at.tzinfo is not None for s in snaps))
        self.assertTrue(all(s.recorded_at.utcoffset().total_seconds() == 0 for s in snaps))

    def test_naive_timestamp_is_taken_as_utc(self):
        snaps, _ = parse_tracker_history(self.payload)
        naive = [s for s in snaps if s.playlist_id == 13]
        self.assertEqual(naive[0].recorded_at, datetime(2026, 6, 1, 12, tzinfo=timezone.utc))

    def test_offset_is_converted_to_utc(self):
        self.assertEqual(parse_timestamp("2026-06-01T14:00:00+02:00"),
                         datetime(2026, 6, 1, 12, tzinfo=timezone.utc))

    def test_tier_fields_are_kept_when_present(self):
        snaps, _ = parse_tracker_history(self.payload)
        unranked = next(s for s in snaps if s.playlist_id == 0)
        self.assertEqual((unranked.tier, unranked.division, unranked.tier_id, unranked.division_id),
                         ("Unranked", "Division I", 0, 0))

    def test_rejects_wrong_shape(self):
        with self.assertRaises(HistoryFormatError):
            parse_tracker_history([1, 2, 3])
        with self.assertRaises(HistoryFormatError):
            parse_tracker_history({"data": []})

    def test_non_integer_rating_is_skipped(self):
        snaps, skipped = parse_tracker_history(
            {"data": {"11": [{"rating": True, "collectDate": "2026-01-01T00:00:00Z"},
                             {"rating": 12.5, "collectDate": "2026-01-01T00:00:00Z"}]}})
        self.assertEqual(snaps, [])
        self.assertEqual(skipped, 2)

    def test_same_timestamp_different_ratings_are_both_kept(self):
        snaps, skipped = parse_tracker_history(
            {"data": {"11": [{"rating": 500, "collectDate": "2026-01-01T00:00:00Z"},
                             {"rating": 510, "collectDate": "2026-01-01T00:00:00Z"}]}})
        self.assertEqual(len(snaps), 2)
        self.assertEqual(skipped, 0)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.points = [
            (datetime(2026, 1, 5, 8, tzinfo=timezone.utc), 100),
            (datetime(2026, 1, 5, 20, tzinfo=timezone.utc), 110),
            (datetime(2026, 1, 6, 9, tzinfo=timezone.utc), 105),
            (datetime(2026, 2, 1, 9, tzinfo=timezone.utc), 120),
        ]

    def test_raw_keeps_everything(self):
        self.assertEqual(len(aggregate(self.points, "raw")), 4)

    def test_daily_keeps_last_reading_with_its_real_time(self):
        out = aggregate(self.points, "daily")
        self.assertEqual(out, [(datetime(2026, 1, 5, 20, tzinfo=timezone.utc), 110),
                               (datetime(2026, 1, 6, 9, tzinfo=timezone.utc), 105),
                               (datetime(2026, 2, 1, 9, tzinfo=timezone.utc), 120)])

    def test_monthly(self):
        out = aggregate(self.points, "monthly")
        self.assertEqual([v for _, v in out], [105, 120])

    def test_weekly(self):
        out = aggregate(self.points, "weekly")
        self.assertEqual([v for _, v in out], [105, 120])

    def test_unknown_granularity(self):
        with self.assertRaises(ValueError):
            aggregate(self.points, "hourly")


# ====================================================================================================
# From test_ranks_and_sessions.py
# ====================================================================================================
def t(day, hour=0, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


class RankNameTests(unittest.TestCase):
    def test_tier_names(self):
        self.assertEqual(tier_name(0), "Unranked")
        self.assertEqual(tier_name(12), "Platinum III")
        self.assertEqual(tier_name(15), "Diamond III")
        self.assertEqual(tier_name(22), "Supersonic Legend")
        self.assertEqual(tier_name(99), "Tier 99")
        self.assertEqual(tier_name(None), "")

    def test_label_from_ids_matches_stored_names(self):
        # MMR API readings have IDs only; Tracker rows have names. Both must read the same.
        self.assertEqual(rank_label(12, 1), "Platinum III Div II")
        self.assertEqual(rank_label(12, 1, "Platinum III", "Division II"), "Platinum III Div II")

    def test_label_without_divisions(self):
        self.assertEqual(rank_label(0, 0), "Unranked")
        self.assertEqual(rank_label(22, 0), "Supersonic Legend")
        self.assertEqual(rank_label(None, None), "")


class RankChangeTests(unittest.TestCase):
    def test_detects_promotions_and_demotions(self):
        rows = [
            (t(3), 780, 12, 2, "", ""),
            (t(1), 760, 12, 1, "", ""),
            (t(2), 765, 12, 1, "", ""),  # unchanged, no entry
            (t(4), 800, 13, 0, "", ""),  # tier promotion
            (t(5), 790, 12, 3, "", ""),  # tier demotion
        ]
        changes = rank_changes(rows)
        self.assertEqual([c.direction for c in changes], [1, 1, -1])
        self.assertEqual([c.tier_changed for c in changes], [False, True, True])
        self.assertEqual(changes[0].before, "Platinum III Div II")
        self.assertEqual(changes[0].after, "Platinum III Div III")
        self.assertEqual(changes[1].when, t(4))

    def test_rows_without_tier_are_ignored(self):
        rows = [(t(1), 700, 12, 1, "", ""), (t(2), 720, None, None, "", ""), (t(3), 730, 12, 1, "", "")]
        self.assertEqual(rank_changes(rows), [])


class RankDistanceTests(unittest.TestCase):
    def test_next_division_steps(self):
        from rlutils.data import next_division, prev_division
        self.assertEqual(next_division(15, 2), (15, 3))
        self.assertEqual(next_division(15, 3), (16, 0))
        self.assertEqual(next_division(21, 1), (21, 2))  # GC III Div II -> Div III
        self.assertEqual(next_division(21, 3), (22, 0))  # -> SSL
        self.assertIsNone(next_division(22, 0))
        self.assertEqual(prev_division(16, 0), (15, 3))
        self.assertIsNone(prev_division(1, 0))

    def test_estimate_from_lowest_reading_seen(self):
        from rlutils.data import rank_distance
        readings = [(1029, 15, 2), (1015, 15, 2), (1041, 15, 3), (1047, 15, 3), (1060, 16, 0)]
        d = rank_distance(1029, 15, 2, readings)
        self.assertEqual((d.next_label, d.to_next), ("Diamond III Div IV", 12))
        self.assertEqual((d.prev_label, d.to_drop), ("Diamond III Div II", 14))

    def test_unknown_next_division(self):
        from rlutils.data import rank_distance
        d = rank_distance(1029, 15, 2, [(1029, 15, 2)])
        self.assertIsNone(d.to_next)
        self.assertIsNone(rank_distance(1029, None, None, []))


class RankTableTests(unittest.TestCase):
    def setUp(self):
        from rlutils.data import load_thresholds
        self.table = load_thresholds()[11]

    def test_bundled_doubles_table_loads_without_unranked(self):
        self.assertEqual(self.table[0][:2], (1, 0))
        self.assertEqual(len(self.table), 85)

    def test_rank_lookup_including_gaps(self):
        from rlutils.data import rank_for_mmr
        self.assertEqual(rank_for_mmr(self.table, 1029), (15, 2))  # Diamond III Div III
        self.assertEqual(rank_for_mmr(self.table, 1027), (15, 1))
        self.assertEqual(rank_for_mmr(self.table, 1070), (15, 3))  # gap 1065-1074 stays Div IV
        self.assertEqual(rank_for_mmr(self.table, 1075), (16, 0))  # Champion I
        self.assertEqual(rank_for_mmr(self.table, 2100), (22, 0))

    def test_exact_distance_from_table(self):
        from rlutils.data import rank_distance
        d = rank_distance(1029, 15, 2, [], self.table)
        self.assertFalse(d.estimated)
        self.assertEqual((d.next_label, d.to_next, d.to_drop), ("Diamond III Div IV", 23, 1))
        champ = rank_distance(1064, 15, 3, [], self.table)
        self.assertEqual((champ.next_label, champ.to_next), ("Champion I Div I", 11))

    def test_whole_rank_distance_from_table(self):
        from rlutils.data import tier_distance
        d = tier_distance(1058, 15, [], self.table)  # Diamond III Div IV
        d3_start = min(low for t, _d, low, _h in self.table if t == 15)
        self.assertEqual((d.next_label, d.to_next), ("Champion I", 17))  # Champion I starts at 1075
        self.assertEqual((d.prev_label, d.to_drop), ("Diamond II", 1058 - d3_start))
        self.assertFalse(d.estimated)

    def test_whole_rank_distance_estimated_without_table(self):
        from rlutils.data import tier_distance
        readings = [(760, 12, 3), (771, 13, 0), (790, 13, 1), (850, 14, 0)]  # Platinum III, Diamond I/II
        d = tier_distance(800, 13, readings)  # Diamond I
        self.assertEqual((d.next_label, d.to_next), ("Diamond II", 50))
        self.assertEqual((d.prev_label, d.to_drop), ("Platinum III", 29))
        self.assertTrue(d.estimated)
        self.assertIsNone(tier_distance(800, 13, [(800, 13, 0)]).to_next)  # next rank never seen

    def test_parses_tracker_format(self):
        from rlutils.data import parse_tracker_thresholds
        payload = {"data": {"data": [{"tier": 15, "division": 2, "playlist": 13, "minMMR": 980, "maxMMR": 1000},
                                     {"tier": "x"}]}}
        self.assertEqual(parse_tracker_thresholds(payload), {13: [(15, 2, 980, 1000)]})
        with self.assertRaises(ValueError):
            parse_tracker_thresholds({"nope": 1})


class SessionTests(unittest.TestCase):
    def m(self, when, won, playlist=11, delta=None):
        return {"ended_at": when.isoformat(), "won": int(won), "playlist_id": playlist, "mmr_delta": delta}

    def test_split_by_gap_newest_first(self):
        matches = [
            self.m(t(1, 20, 0), True, delta=9),
            self.m(t(1, 20, 10), False, delta=-8),
            self.m(t(1, 20, 20), True, playlist=0),  # casual: W/L only
            self.m(t(2, 18, 0), False, playlist=10, delta=-10),
        ]
        sessions = split_sessions(matches, timedelta(minutes=30))
        self.assertEqual(len(sessions), 2)
        latest, earlier = sessions
        self.assertEqual(latest.start, t(2, 18, 0))
        self.assertEqual((earlier.start, earlier.end), (t(1, 20, 0), t(1, 20, 20)))
        self.assertEqual(earlier.summary["wins"], 2)
        doubles = earlier.playlists[11]
        self.assertEqual((doubles.wins, doubles.losses, doubles.mmr_delta, doubles.logged_deltas), (1, 1, 1, 2))
        self.assertEqual(earlier.playlists[0].logged_deltas, 0)

    def test_empty(self):
        self.assertEqual(split_sessions([], timedelta(minutes=30)), [])


# ====================================================================================================
# From test_screenread.py
# ====================================================================================================
ME = "Epic|0123456789abcdef0123456789abcdef|0"




class LedgerTests(unittest.TestCase):
    """MMR comes only from saved readings; the ledger turns them into per-match changes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "l.db"
        data.upsert_profile("T", ME, path=self.path)
        self.t0 = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def snap(self, rating, minutes, source="screen", playlist=11):
        from rlutils.data import Snapshot
        data.insert_snapshots("T", [Snapshot(playlist, rating, "", "", None, None,
                                           self.t0 + timedelta(minutes=minutes), source)], path=self.path)

    def match(self, key, minutes, playlist=11):
        return data.insert_match("T", key, playlist, True, (self.t0 + timedelta(minutes=minutes)).isoformat(),
                               path=self.path)

    def row(self, mid):
        return next(m for m in data.list_matches("T", path=self.path) if m["id"] == mid)

    def ledger(self, hours_later: float = 1):
        from rlutils.data import MmrLedger
        return MmrLedger("T", db_path=self.path, now=lambda: self.t0 + timedelta(hours=hours_later))

    def test_old_unread_session_is_not_lumped_into_the_next(self):
        self.snap(1029, 0)
        self.match("last-night", 8)
        lg = self.ledger(hours_later=20)
        self.assertEqual(lg.waiting_count(), 0)
        tonight = self.match("tonight", 20 * 60)
        lg.add_match(11, tonight, self.t0 + timedelta(hours=20))
        first = lg.reading(11, 1040, "screen")
        self.assertEqual([u["delta"] for u in first], [None])  # baseline unknown: no made-up change
        nxt = self.match("tonight-2", 20 * 60 + 9)
        lg.add_match(11, nxt, self.t0 + timedelta(hours=20, minutes=9))
        self.assertEqual(lg.reading(11, 1048, "screen")[0]["delta"], 8)

    def test_one_match_exact_change(self):
        self.snap(1029, 0)
        lg = self.ledger()
        mid = self.match("g1", 8)
        lg.add_match(11, mid, self.t0 + timedelta(minutes=8))
        updates = lg.reading(11, 1041, "screen")
        self.assertEqual([(u["delta"], u["covered"]) for u in updates], [(12, 1)])
        r = self.row(mid)
        self.assertEqual((r["mmr_before"], r["mmr_after"], r["mmr_delta"], r["mmr_matches"]), (1029, 1041, 12, None))
        self.assertEqual(lg.waiting_count(), 0)

    def test_several_matches_share_one_change(self):
        self.snap(1029, 0)
        lg = self.ledger()
        ids = [self.match(f"g{i}", 8 * i) for i in (1, 2, 3)]
        for i, mid in zip((1, 2, 3), ids):
            lg.add_match(11, mid, self.t0 + timedelta(minutes=8 * i))
        lg.reading(11, 1020, "screen")
        last = self.row(ids[-1])
        self.assertEqual((last["mmr_before"], last["mmr_after"], last["mmr_delta"], last["mmr_matches"]),
                         (1029, 1020, -9, 3))
        self.assertIsNone(self.row(ids[0])["mmr_delta"])

    def test_old_number_still_on_screen_keeps_waiting(self):
        self.snap(1029, 0)
        lg = self.ledger()
        mid = self.match("g1", 8)
        lg.add_match(11, mid, self.t0 + timedelta(minutes=8))
        self.assertEqual(lg.reading(11, 1029, "screen"), [])
        self.assertEqual(lg.waiting_count(11), 1)
        self.assertEqual(lg.reading(11, 1037, "screen")[0]["delta"], 8)

    def test_restart_rebuilds_waiting_and_settles_bracketed_matches(self):
        self.snap(1029, 0)
        a = self.match("a", 8)
        b = self.match("b", 16)
        self.snap(1045, 20)        # a reading after a and b (e.g. typed in while the overlay was off)
        c = self.match("c", 30)    # after the latest reading: still waiting
        lg = self.ledger()
        r = self.row(b)
        self.assertEqual((r["mmr_before"], r["mmr_after"], r["mmr_delta"], r["mmr_matches"]), (1029, 1045, 16, 2))
        self.assertEqual(lg.waiting_count(11), 1)
        self.assertEqual(lg.expected(11), 1045)
        lg.reading(11, 1052, "screen")
        self.assertEqual(self.row(c)["mmr_delta"], 7)
        self.assertIsNone(self.row(a)["mmr_delta"])

    def test_restart_leaves_partly_settled_groups_alone(self):
        self.snap(1000, 0)
        a = self.match("a", 8)
        data.set_match_mmr(a, 1000, 1010, path=self.path)
        self.match("b", 16)
        self.snap(1030, 20)
        self.ledger()
        self.assertEqual(self.row(a)["mmr_delta"], 10)
        self.assertEqual([m["mmr_delta"] for m in data.list_matches("T", path=self.path)].count(None), 1)








ME = "Epic|0123456789abcdef0123456789abcdef|0"










# ====================================================================================================
# From test_streaks_and_db.py
# ====================================================================================================
class StreakTests(unittest.TestCase):
    def test_no_results(self):
        self.assertEqual(current_streak([]), 0)
        self.assertIsNone(summarize([])["win_ratio"])

    def test_current_streak_signs(self):
        self.assertEqual(current_streak([False, True, True, True]), 3)
        self.assertEqual(current_streak([True, False, False]), -2)

    def test_longest_streaks(self):
        results = [True, True, False, False, False, True]
        self.assertEqual(longest_streaks(results), (2, 3))

    def test_summary(self):
        s = summarize([True, True, False, True])
        self.assertEqual((s["wins"], s["losses"]), (3, 1))
        self.assertAlmostEqual(s["win_ratio"], 0.75)
        self.assertEqual(s["current_streak"], 1)




class DbTests(unittest.TestCase):
    def test_profile_for_epic_account(self):
        db = self.path
        data.upsert_profile("Main", "Epic|0a1b2c3d4e5f60718293a4b5c6d7e8f9|0", db)
        self.assertEqual(data.profile_for_epic_account("0A1B2C3D4E5F60718293A4B5C6D7E8F9", "Renamed", db), "Main")
        key = data.profile_for_epic_account("f9e8d7c6b5a40312a1b2c3d4e5f60718", "Main", db)  # name taken
        self.assertEqual(key, "Main (f9e8d7)")
        self.assertEqual(data.get_profile(key, db)["player_id"], "Epic|f9e8d7c6b5a40312a1b2c3d4e5f60718|0")


    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        data.upsert_profile("Tester", "Epic|abc|0", path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def snap(self, rating, hour, source=SOURCE_TRACKER_IMPORT, playlist=11):
        return Snapshot(playlist, rating, "", "", None, None,
                        datetime(2026, 1, 1, hour, tzinfo=timezone.utc), source)

    def test_profile_roundtrip_keeps_player_id(self):
        data.upsert_profile("Tester", None, path=self.path)  # must not erase the ID
        self.assertEqual(data.get_profile("Tester", path=self.path)["player_id"], "Epic|abc|0")

    def test_reimport_is_idempotent(self):
        snaps = [self.snap(500, 1), self.snap(510, 2)]
        self.assertEqual(data.insert_snapshots("Tester", snaps, path=self.path), 2)
        self.assertEqual(data.insert_snapshots("Tester", snaps, path=self.path), 0)
        self.assertEqual(len(data.list_snapshots("Tester", 11, path=self.path)), 2)

    def test_same_time_distinct_rating_is_kept(self):
        added = data.insert_snapshots("Tester", [self.snap(500, 1), self.snap(510, 1)], path=self.path)
        self.assertEqual(added, 2)

    def test_same_rating_from_different_sources_is_kept(self):
        data.insert_snapshots("Tester", [self.snap(500, 1, SOURCE_TRACKER_IMPORT)], path=self.path)
        added = data.insert_snapshots("Tester", [self.snap(500, 1, SOURCE_CURRENT)], path=self.path)
        self.assertEqual(added, 1)

    def test_latest_rating_and_playlists(self):
        data.insert_snapshots("Tester", [self.snap(500, 1), self.snap(530, 5), self.snap(10, 3, playlist=13)],
                            path=self.path)
        self.assertEqual(data.latest_rating("Tester", 11, path=self.path), 530)
        self.assertEqual(data.playlists_with_snapshots("Tester", path=self.path), [11, 13])

    def test_match_insert_dedupes_and_mmr_fills_in_later(self):
        first = data.insert_match("Tester", "guid-1", 11, True, "2026-01-01T10:00:00+00:00", path=self.path)
        again = data.insert_match("Tester", "guid-1", 11, True, "2026-01-01T10:00:00+00:00", path=self.path)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        data.set_match_mmr(first, 1000, 1012, path=self.path)
        row = data.list_matches("Tester", 11, path=self.path)[0]
        self.assertEqual((row["mmr_before"], row["mmr_after"], row["mmr_delta"]), (1000, 1012, 12))


# ====================================================================================================
# Launch: the account switcher's output
# ====================================================================================================


class SwitcherOutputTests(unittest.TestCase):
    def test_accounts_parsed(self):
        from unittest import mock
        from rlutils import game
        out = 'noise\n[{"Id":"5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a","Name":"Alt","Active":false},' \
              '{"Id":"0a1b2c3d4e5f60718293a4b5c6d7e8f9","Name":"Main","Active":true}]\n'
        done = mock.Mock(returncode=0, stdout=out, stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(game, "_powershell_file", return_value=done):
            script = Path(d) / "EpicSwitcher.ps1"
            script.write_text("", encoding="utf-8")
            rows = game.switcher_accounts(script)
        self.assertEqual([(r["name"], r["active"]) for r in rows], [("Alt", False), ("Main", True)])

    def test_failed_switch_reports_message(self):
        from unittest import mock
        from rlutils import game
        failed = mock.Mock(returncode=3, stdout="Switching\nThe launcher didn't sign in as Main.\n", stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(game, "_powershell_file", return_value=failed):
            script = Path(d) / "EpicSwitcher.ps1"
            script.write_text("", encoding="utf-8")
            with self.assertRaises(game.ReadError) as ctx:
                game.switch_account(script, "0a1b2c3d4e5f60718293a4b5c6d7e8f9", 5)
        self.assertIn("didn't sign in", str(ctx.exception))

    def test_add_and_save_pass_their_options(self):
        from unittest import mock
        from rlutils import game
        ok = mock.Mock(returncode=0, stdout="Saved Main.\n", stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(game, "_powershell_file", return_value=ok) as ps:
            script = Path(d) / "EpicSwitcher.ps1"
            script.write_text("", encoding="utf-8")
            self.assertEqual(game.save_current_account(script), "Saved Main.")
            self.assertEqual(ps.call_args.args[1:], ("-SaveCurrent",))
            game.add_new_account(script)
            self.assertEqual(ps.call_args.args[1:], ("-AddNew",))
        nothing = mock.Mock(returncode=4, stdout="The launcher isn't signed in with \"Remember me\" ticked.\n", stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(game, "_powershell_file", return_value=nothing):
            script = Path(d) / "EpicSwitcher.ps1"
            script.write_text("", encoding="utf-8")
            with self.assertRaises(game.ReadError) as ctx:
                game.save_current_account(script)
        self.assertIn("isn't signed in", str(ctx.exception))


class VibranceTests(unittest.TestCase):
    def test_without_nvidia_it_turns_itself_off(self):
        from unittest import mock
        from PySide6.QtCore import QObject
        from rlutils import vibrance as v

        class Window(QObject):
            cfg = {"vibrance": {"pct": 90, "obs": False, "obs_strength": 1.0, "obs_url": "", "obs_password": ""}}

        def no_driver():
            raise v.VibranceError("needs an NVIDIA graphics card (no NVIDIA driver found), so it stays off")

        with mock.patch.object(v, "Nvapi", no_driver):
            w = app.VibranceWatcher(Window())
        w.obs_synced = 0.0  # OBS off and already in step: no connection attempt
        w.tick()            # no driver: nothing to set, no error
        w.restore()
        self.assertIsNone(w.nv)
        self.assertIn("NVIDIA graphics card", w.screen_state)

    def test_percent_and_levels(self):
        from rlutils import vibrance as v
        self.assertEqual([v.pct_to_level(p, 0, 63) for p in (40, 50, 75, 100, 120)], [0, 0, 32, 63, 63])
        self.assertEqual(v.level_to_pct(32, 0, 63), 75)

    def test_obs_saturation(self):
        from rlutils import vibrance as v
        self.assertEqual(v.pct_to_obs_saturation(50), 0.0)
        self.assertEqual(v.pct_to_obs_saturation(75), 0.5)
        self.assertEqual(v.pct_to_obs_saturation(100, strength=1.5), 1.5)

    def test_old_lens_settings_are_dropped(self):
        from rlutils import data
        self.assertNotIn("mode", data.DEFAULTS["vibrance"])
        self.assertIn("lens_hz", data.OBSOLETE_SUBKEYS["vibrance"])


# ====================================================================================================
# MMR from Rocket League's log (rlutils/rllog.py): parsing, files (rotation, partial lines, restarts), saving
# ====================================================================================================
from rlutils import importer, rllog

LOG_FIXTURE = Path(__file__).parent / "fixtures" / "rl_launch_sample.log"   # real line shapes, made-up ids
PROFILE_FIXTURE = Path(__file__).parent / "fixtures" / "tracker_profile_sample.json"
ACC = "a" * 32
OTHER = "b" * 32


def line(t, text):
    return f"[{t:07.2f}] {text}\n"


def me(name="Solo Tester", acc=ACC):
    return line(1, f"Party: HandleLocalPlayerLoginStatusChanged PlayerName={name} PlayerID=Epic|{acc}|0 "
                   "LoginStatus=LS_LoggedIn IsPrimary=True IsInParty=False")


def queue(skill, at, playlists="11", t=10.0):
    return (line(t, f"Matchmaking: Pre-divide PartyLeaderMMR: {skill}")
            + line(t, f"Matchmaking: Post-divide PartyLeaderMMR: {skill}")
            + line(t, "Matchmaking: PartyLeaderTier=(15)")
            + line(t, f"Matchmaking: StartMatchmaking at {at} in USE3,USE1 for playlists {playlists} on game server "))


def parse(text):
    p = rllog.LogParser()
    return [r for r in (p.feed(x) for x in text.splitlines()) if r is not None], p


class LogParseTests(unittest.TestCase):
    def test_skill_to_mmr_matches_the_game(self):
        # skill values from the real log next to the MMR the game showed (Shift OCR / in-game, 2026-10-19)
        for skill, shown in ((35.7555, 815), (36.3547, 827), (37.0589, 841), (46.1306, 1023), (44.7251, 995),
                             (31.8253, 737), (25.0, 600)):
            self.assertEqual(rllog.skill_to_mmr(skill), shown, skill)
        self.assertEqual(rllog.skill_to_mmr(45.025), 1001)   # exactly .5 rounds up
        self.assertEqual(rllog.skill_to_mmr(45.0249), 1000)

    def test_fixture_gives_ranked_readings_only(self):
        readings, _ = parse(LOG_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual([(r.playlist_id, r.mmr) for r in readings], [(11, 815), (11, 827), (10, 737)])
        first = readings[0]
        self.assertEqual(first.queued_at, datetime(2026, 10, 19, 5, 0, 43, tzinfo=timezone.utc))
        self.assertEqual((first.account, first.name, first.members), (ACC, "Solo Tester", 1))
        self.assertTrue(all(r.attributable for r in readings))

    def test_playlist_mapping(self):
        text = me() + "".join(queue(40, f"2026-10-19 05:0{i}:00", pl) for i, pl in
                              enumerate(["10", "11", "13", "2", "27", "61", "10,11"]))
        readings, _ = parse(text)
        self.assertEqual([r.playlist_id for r in readings], [10, 11, 13])  # casual, extra modes, multi skipped

    def test_queue_without_its_skill_line_gives_nothing(self):
        readings, _ = parse(me() + line(5, "Matchmaking: StartMatchmaking at 2026-10-19 05:00:00 in USE1 "
                                           "for playlists 11 on game server "))
        self.assertEqual(readings, [])

    def test_skill_line_is_used_once(self):
        text = me() + queue(40, "2026-10-19 05:00:00") + line(9, "Matchmaking: StartMatchmaking at "
                                                              "2026-10-19 05:01:00 in USE1 for playlists 11 on ")
        readings, _ = parse(text)
        self.assertEqual(len(readings), 1)

    def test_no_signed_in_player_is_flagged(self):
        readings, _ = parse(queue(40, "2026-10-19 05:00:00"))
        self.assertFalse(readings[0].attributable)
        self.assertIn("account", readings[0].reason)

    def test_own_party_is_attributed(self):
        text = (me() + line(2, "Party: OnPartyCreated bWasSuccessful=(True) InPartyId:'PsyNet-A' ")
                + line(2, "Party: HandleJoinLobby bWasSuccessful=(True) LobbyUID:'PsyNet-A' ")
                + line(2, f"Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-A Members=1 MemberIndex=0 MemberID=Epic|{ACC}|0")
                + line(3, f"Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-A Members=2 MemberIndex=1 MemberID=Epic|{OTHER}|0")
                + queue(35.7555, "2026-10-19 05:00:43"))
        (r,), _ = parse(text)
        self.assertEqual((r.members, r.attributable, r.mmr), (2, True, 815))

    def test_someone_elses_party_is_flagged_until_you_lead_or_are_alone(self):
        text = (me() + line(2, "Party: HandleJoinLobby bWasSuccessful=(True) LobbyUID:'PsyNet-X' ")
                + line(2, "Party: OnPartyLeaderChanged NewLeader=XboxOne|1234567890123456|0")
                + line(2, "Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-X Members=2 MemberIndex=0 MemberID=XboxOne|1234567890123456|0")
                + line(2, f"Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-X Members=2 MemberIndex=1 MemberID=Epic|{ACC}|0")
                + queue(50, "2026-10-19 05:00:00")
                + line(4, f"Party: OnPartyLeaderChanged NewLeader=Epic|{ACC}|0")
                + queue(41, "2026-10-19 05:10:00")
                + line(5, "Party: OnPartyLeaderChanged NewLeader=XboxOne|1234567890123456|0")
                + line(5, "Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-X Members=1 MemberIndex=-1 MemberID= Status=Left")
                + line(5, "Party: Back down to one player, start party timeout")
                + queue(42, "2026-10-19 05:20:00"))
        readings, _ = parse(text)
        self.assertEqual([(r.attributable, r.reason) for r in readings],
                         [(False, "party led by someone else"), (True, ""), (True, "")])

    def test_unknown_leader_is_flagged(self):
        text = (me() + line(2, "Party: HandleJoinLobby bWasSuccessful=(True) LobbyUID:'PsyNet-X' ")
                + line(2, "Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-X Members=3 MemberIndex=2 MemberID=Epic|cccccccccccccccccccccccccccccccc|0")
                + queue(50, "2026-10-19 05:00:00"))
        (r,), _ = parse(text)
        self.assertEqual((r.attributable, r.reason), (False, "party leader unknown"))

    def test_first_member_of_a_joined_lobby_is_its_leader(self):
        text = (me() + line(2, "Party: HandleJoinLobby bWasSuccessful=(True) LobbyUID:'PsyNet-X' ")
                + line(2, f"Party: HandleLobbyMemberStatusUpdate LobbyUID=PsyNet-X Members=2 MemberIndex=0 MemberID=Epic|{OTHER}|0")
                + queue(50, "2026-10-19 05:00:00"))
        (r,), _ = parse(text)
        self.assertEqual(r.reason, "party led by someone else")

    def test_state_round_trip_keeps_a_waiting_skill_line(self):
        p = rllog.LogParser()
        for x in (me() + line(5, "Matchmaking: Pre-divide PartyLeaderMMR: 35.7555")).splitlines():
            p.feed(x)
        q = rllog.LogParser(json.loads(json.dumps(p.state())))
        r = q.feed(line(5, "Matchmaking: StartMatchmaking at 2026-10-19 05:00:43 in USW3 for playlists 11 on "))
        self.assertEqual((r.mmr, r.account), (815, ACC))

    def test_exchange_code_line_is_never_kept(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Launch.log").write_bytes(LOG_FIXTURE.read_bytes())
            tailer = rllog.LogTailer(Path(d))
            tailer.poll()
            tailer.commit()
            dumped = json.dumps(tailer.state())
        self.assertNotIn("AUTH_PASSWORD", dumped)
        self.assertNotIn("0000fake0exchange", dumped)


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.log = self.dir / "Launch.log"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text, path=None, mode="a"):
        with open(path or self.log, mode + "b") as f:
            f.write(text.encode("utf-8"))

    def head(self, stamp="19/10/2026 07:45:24"):
        return f"Log: Log file open, {stamp}\nLog: Command line: -AUTH_PASSWORD=fake\n" + me()

    def test_reads_only_new_lines(self):
        self.write(self.head() + queue(35.7555, "2026-10-19 05:00:43"))
        t = rllog.LogTailer(self.dir)
        self.assertEqual([r.mmr for r in t.poll()], [815])
        t.commit()
        self.assertEqual(t.poll(), [])
        t.commit()
        self.write(queue(36.3547, "2026-10-19 05:09:56"))
        self.assertEqual([r.mmr for r in t.poll()], [827])

    def test_partial_line_waits_for_the_rest(self):
        self.write(self.head() + line(5, "Matchmaking: Pre-divide PartyLeaderMMR: 35.7555")
                   + "[0005.00] Matchmaking: StartMatchmaking at 2026-10-19 05:00:43 in USW3 for playl")
        t = rllog.LogTailer(self.dir)
        self.assertEqual(t.poll(), [])   # the game writes in blocks: half a line isn't read
        t.commit()
        self.write("ists 11 on game server \n")
        self.assertEqual([r.mmr for r in t.poll()], [815])

    def test_file_without_a_complete_first_line_is_left_for_later(self):
        self.write("Log: Log file op")
        t = rllog.LogTailer(self.dir)
        self.assertEqual(t.poll(), [])
        t.commit()
        self.assertEqual(t.state()["files"], {})

    def test_rotation_finishes_the_old_file_and_reads_the_new_one(self):
        self.write(self.head() + queue(35.7555, "2026-10-19 05:00:43"))
        t = rllog.LogTailer(self.dir)
        t.poll()
        t.commit()
        # the game wrote one more queue, then a new start renamed Launch.log and began a new one
        self.write(queue(36.3547, "2026-10-19 05:09:56"))
        self.log.rename(self.dir / "Launch-backup-2026.10.19-07.44.54.log")
        self.write(self.head("19/10/2026 15:08:28") + queue(46.1306, "2026-10-19 16:35:32"), mode="w")
        self.assertEqual(sorted(r.mmr for r in t.poll()), [827, 1023])  # nothing read twice
        t.commit()
        self.assertEqual(t.poll(), [])
        self.assertEqual(len(t.state()["files"]), 2)

    def test_restart_carries_on_from_saved_state(self):
        state_file = self.dir / "state.json"
        self.write(self.head() + queue(35.7555, "2026-10-19 05:00:43")
                   + line(9, "Matchmaking: Pre-divide PartyLeaderMMR: 36.3547"))
        t = rllog.LogTailer(self.dir)
        self.assertEqual(len(t.poll()), 1)
        t.commit()
        rllog.save_state(state_file, t.state())
        self.write(line(9, "Matchmaking: StartMatchmaking at 2026-10-19 05:09:56 in USW3 for playlists 11 on "))
        again = rllog.LogTailer(self.dir, rllog.load_state(state_file))   # app restarted
        self.assertEqual([r.mmr for r in again.poll()], [827])          # the waiting skill line was kept

    def test_not_committed_means_read_again(self):
        self.write(self.head() + queue(35.7555, "2026-10-19 05:00:43"))
        t = rllog.LogTailer(self.dir)
        self.assertEqual(len(t.poll()), 1)   # saving failed: no commit
        self.assertEqual(len(t.poll()), 1)

    def test_bad_or_old_state_starts_over(self):
        self.write(self.head() + queue(35.7555, "2026-10-19 05:00:43"))
        for state in (None, {}, {"version": 999, "files": {"x": {"offset": 5}}}, "junk"):
            self.assertEqual(len(rllog.LogTailer(self.dir, state).poll()), 1)
        broken = self.dir / "s.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertIsNone(rllog.load_state(broken))

    def test_shorter_file_with_same_first_line_is_read_from_the_start(self):
        self.write(self.head() + queue(35.7555, "2026-10-19 05:00:43") + queue(36.3547, "2026-10-19 05:09:56"))
        t = rllog.LogTailer(self.dir)
        t.poll()
        t.commit()
        self.write(self.head() + queue(37.0589, "2026-10-19 05:17:02"), mode="w")
        self.assertEqual([r.mmr for r in t.poll()], [841])

    def test_files_in_order_and_only_launch_logs(self):
        self.write(self.head(), self.dir / "Launch-backup-2026.10.18-02.07.18.log", "w")
        self.write(self.head("x"))
        self.write("x", self.dir / "other.txt", "w")
        names = [p.name for p in rllog.log_files(self.dir)]
        self.assertEqual(set(names), {"Launch.log", "Launch-backup-2026.10.18-02.07.18.log"})
        self.assertEqual(rllog.log_files(self.dir / "missing"), [])


class LogSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"

    def tearDown(self):
        self._tmp.cleanup()

    def r(self, mmr, minute, pl=11, ok=True, acc=ACC, reason=""):
        return rllog.Reading(pl, mmr, (mmr - 100) / 20, datetime(2026, 10, 19, 5, minute, tzinfo=timezone.utc),
                             acc, "Solo Tester", 1, ok, reason)

    def test_saves_to_the_accounts_profile_with_source_rl_log(self):
        res = rllog.save([self.r(815, 0), self.r(827, 9), self.r(737, 20, pl=10)], self.db)
        self.assertEqual(res.saved, 3)
        key = data.profile_for_epic_account(ACC, "", self.db)
        self.assertEqual(key, "Solo Tester")
        rows = data.list_snapshots(key, 11, self.db)
        self.assertEqual([(x["rating"], x["source"], x["tier_id"]) for x in rows],
                         [(815, data.SOURCE_RL_LOG, None), (827, data.SOURCE_RL_LOG, None)])
        self.assertEqual(rows[0]["recorded_at"], "2026-10-19T05:00:00+00:00")
        self.assertEqual(res.last[1].mmr, 737)

    def test_existing_profile_of_the_account_is_used(self):
        data.upsert_profile("Main", f"Epic|{ACC}|0", self.db)
        rllog.save([self.r(815, 0)], self.db)
        self.assertEqual(len(data.list_snapshots("Main", 11, self.db)), 1)

    def test_flagged_readings_are_counted_not_saved(self):
        res = rllog.save([self.r(900, 0, ok=False, reason="party led by someone else"),
                          self.r(901, 1, ok=False, reason="party led by someone else"),
                          self.r(902, 2, ok=False, acc=None, reason="signed-in Epic account not found in the log yet")],
                         self.db)
        self.assertEqual(res.saved, 0)
        self.assertEqual(res.skipped, {"party led by someone else": 2,
                                       "signed-in Epic account not found in the log yet": 1})
        self.assertEqual(data.list_profiles(self.db), [])

    def test_requeue_with_same_mmr_and_resave_add_nothing(self):
        batch = [self.r(1023, 0), self.r(1023, 9), self.r(1013, 17)]
        first = rllog.save(batch, self.db)
        self.assertEqual((first.saved, first.unchanged), (2, 1))
        again = rllog.save(batch, self.db)  # e.g. state lost: the same lines read again
        self.assertEqual((again.saved, again.unchanged), (0, 3))
        key = data.profile_for_epic_account(ACC, "", self.db)
        self.assertEqual([x["rating"] for x in data.list_snapshots(key, 11, self.db)], [1023, 1013])

    def test_same_mmr_from_another_source_doesnt_hide_a_log_reading(self):
        key = data.profile_for_epic_account(ACC, "Solo Tester", self.db)
        data.insert_snapshots(key, [Snapshot(11, 815, "", "", None, None,
                                             datetime(2026, 10, 19, 0, tzinfo=timezone.utc))], self.db)
        self.assertEqual(rllog.save([self.r(815, 0)], self.db).saved, 1)

    def test_snapshot_before_compares_times_not_strings(self):
        key = data.profile_for_epic_account(ACC, "Solo Tester", self.db)
        data.insert_snapshots(key, [Snapshot(11, 700, "", "", None, None,
                                             datetime(2026, 10, 19, 5, 30, tzinfo=timezone(timedelta(hours=-6))),
                                             data.SOURCE_RL_LOG)], self.db)
        # 05:30-06:00 is 11:30 UTC: after 06:00 UTC, so not "before" it
        self.assertIsNone(data.snapshot_before(key, 11, datetime(2026, 10, 19, 6, tzinfo=timezone.utc),
                                               data.SOURCE_RL_LOG, self.db))

    def test_fixture_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Launch.log").write_bytes(LOG_FIXTURE.read_bytes())
            t = rllog.LogTailer(Path(d))
            res = rllog.save(t.poll(), self.db)
            t.commit()
            self.assertEqual(res.saved, 3)
            self.assertEqual(rllog.save(t.poll(), self.db).saved, 0)


# ====================================================================================================
# Import history: checking a pasted tracker.gg history or profile (rlutils/importer.py)
# ====================================================================================================


class PasteTests(unittest.TestCase):
    def test_empty_html_and_bad_json_are_explained(self):
        with self.assertRaisesRegex(importer.PasteProblem, "Nothing pasted"):
            importer.parse_paste("   ")
        with self.assertRaisesRegex(importer.PasteProblem, "security check"):
            importer.parse_paste("<!DOCTYPE html><title>Just a moment...</title>")
        with self.assertRaisesRegex(importer.PasteProblem, r"line 1, column \d+.*Raw Data"):
            importer.parse_paste('{"data": {11: []}}')
        with self.assertRaisesRegex(importer.PasteProblem, "cut off"):
            importer.parse_paste('{"data": {"11": [{"rating": 1')

    def test_bom_and_whitespace_are_fine(self):
        self.assertEqual(importer.parse_paste('﻿  {"data": {}}\n'), {"data": {}})

    def test_kind(self):
        self.assertEqual(importer.kind(json.loads(FIXTURE.read_text(encoding="utf-8"))), "history")
        self.assertEqual(importer.kind(json.loads(PROFILE_FIXTURE.read_text(encoding="utf-8"))), "profile")
        self.assertEqual(importer.kind({"errors": [{"message": "We could not find the player x."}]}), "error")
        for other in ({"data": {}}, {"data": {"segments": []}}, {"data": {"11": "x"}}, [1], "x", None):
            self.assertEqual(importer.kind(other), "unknown", other)
        self.assertIn("could not find", importer.error_text({"errors": [{"message": "We could not find the player x."}]}))

    def test_links(self):
        self.assertEqual(importer.history_url("87654321"),
                         "https://api.tracker.gg/api/v1/rocket-league/player-history/mmr/87654321")
        self.assertTrue(importer.profile_url("Some Name").endswith("/standard/profile/epic/Some%20Name"))


class ProfileCheckTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(PROFILE_FIXTURE.read_text(encoding="utf-8"))

    def test_same_epic_account_is_confirmed(self):
        found = importer.check_profile(self.payload, "Whatever Name", f"Epic|{ACC.upper()}|0")
        self.assertEqual((found.tracker_id, found.handle), ("12345678", "Solo Tester"))

    def test_other_epic_account_is_refused(self):
        with self.assertRaisesRegex(importer.PasteProblem, "Solo Tester, not Main"):
            importer.check_profile(self.payload, "Main", f"Epic|{OTHER}|0")

    def test_profile_without_account_matches_by_name(self):
        self.assertEqual(importer.check_profile(self.payload, "solo tester", None).tracker_id, "12345678")
        with self.assertRaises(importer.PasteProblem):
            importer.check_profile(self.payload, "Somebody Else", None)

    def test_missing_player_id_or_other_platform_is_refused(self):
        bad = json.loads(json.dumps(self.payload))
        bad["data"]["metadata"]["playerId"] = None
        with self.assertRaisesRegex(importer.PasteProblem, "no player id"):
            importer.check_profile(bad, "Solo Tester", None)
        steam = json.loads(json.dumps(self.payload))
        steam["data"]["platformInfo"]["platformSlug"] = "steam"
        with self.assertRaisesRegex(importer.PasteProblem, "steam"):
            importer.check_profile(steam, "Solo Tester", None)


class HistoryPreviewTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        data.upsert_profile("Main", f"Epic|{ACC}|0", self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def preview(self, payload=None, hint=""):
        return importer.preview_history(payload or self.payload, "Main", data.all_snapshots("Main", self.db), hint)

    def test_first_import_preview(self):
        p = self.preview()
        self.assertEqual((p.total, len(p.new), p.stored, p.conflicts, p.malformed), (16, 16, 0, 0, 2))
        self.assertEqual([(r.playlist_id, r.name, r.readings, r.new, r.ranked) for r in p.playlists],
                         [(11, "Ranked Doubles 2v2", 14, 14, True), (13, "Ranked Standard 3v3", 1, 1, True),
                          (0, "Casual", 1, 1, False)])
        self.assertEqual((p.first.date().isoformat(), p.last.date().isoformat()), ("2025-12-13", "2026-10-17"))
        self.assertIn("Can't check", p.identity)
        self.assertTrue(all(s.source == data.SOURCE_TRACKER_IMPORT for s in p.new))

    def test_import_then_again_adds_nothing(self):
        data.insert_snapshots("Main", self.preview().new, self.db)
        p = self.preview()
        self.assertEqual((len(p.new), p.stored, p.conflicts), (0, 16, 0))
        self.assertTrue(p.identity.startswith("Matches"))

    def test_changed_day_is_a_conflict_and_stored_value_is_kept(self):
        data.insert_snapshots("Main", self.preview().new, self.db)
        changed = json.loads(json.dumps(self.payload))
        changed["data"]["11"][-1]["rating"] = 1040           # tracker.gg rewrote its newest day
        changed["data"]["11"].append({"rating": 1050, "collectDate": "2026-10-22T00:00:00+00:00"})
        p = self.preview(changed)
        self.assertEqual((len(p.new), p.conflicts), (1, 1))
        self.assertIn("stored 1036, pasted 1040", p.examples[0])
        data.insert_snapshots("Main", p.new, self.db)
        doubles = [r["rating"] for r in data.list_snapshots("Main", 11, self.db)]
        self.assertIn(1036, doubles)
        self.assertNotIn(1040, doubles)
        self.assertEqual(doubles[-1], 1050)

    def test_someone_elses_history_is_refused(self):
        data.insert_snapshots("Main", self.preview().new, self.db)
        other = json.loads(json.dumps(self.payload))
        for rec in other["data"]["11"]:
            rec["rating"] += 300
        with self.assertRaisesRegex(importer.PasteProblem, "doesn't look like Main's"):
            self.preview(other)

    def test_identity_check_uses_log_readings_and_tolerates_rounding(self):
        # readings from other sources on the same days, off by one (rounding): still a match
        days = [("2026-03-13T19:00:00+00:00", 864), ("2026-04-11T10:00:00+00:00", 725),
                ("2026-05-17T22:00:00+00:00", 910)]
        data.insert_snapshots("Main", [Snapshot(11, v, "", "", None, None, parse_timestamp(t), data.SOURCE_RL_LOG)
                                       for t, v in days], self.db)
        p = self.preview()
        self.assertEqual((p.compared, p.agreed), (3, 3))
        self.assertEqual(len(p.new), 16)

    def test_newest_days_dont_count_against_identity(self):
        # only the paste's newest days overlap, and they differ: tracker.gg still changes them, so no refusal
        data.insert_snapshots("Main", [Snapshot(11, 500, "", "", None, None, parse_timestamp(t), data.SOURCE_RL_LOG)
                                       for t in ("2026-10-17T18:00:00+00:00", "2026-10-16T18:00:00+00:00")], self.db)
        self.assertEqual(self.preview().compared, 0)

    def test_hint_is_shown_when_nothing_overlaps(self):
        self.assertEqual(self.preview(hint="From the confirmed profile.").identity, "From the confirmed profile.")

    def test_repeated_reading_in_paste_counts_once(self):
        dup = json.loads(json.dumps(self.payload))
        dup["data"]["11"].append(dict(dup["data"]["11"][0]))
        self.assertEqual(len(self.preview(dup).new), 16)

    def test_unusable_histories(self):
        with self.assertRaisesRegex(importer.PasteProblem, "no MMR readings.*2 records"):
            importer.preview_history({"data": {"13": [{"rating": 1, "collectDate": "x"}, {"collectDate": "y"}]}},
                                     "Main", [])
        with self.assertRaisesRegex(importer.PasteProblem, "isn't a tracker.gg MMR history"):
            importer.preview_history({"data": []}, "Main", [])


# ====================================================================================================
# Games: the Stats API feed, win/loss, storage, playstyle and the win/loss pattern finder
# ====================================================================================================
from rlutils import stats

MY_ID = "Epic|0a1b2c3d4e5f60718293a4b5c6d7e8f9|0"


def msg(event, data, double=True):
    """A Stats API message; Data JSON-encoded inside the JSON, as the game sends it."""
    return json.dumps({"Event": event, "Data": json.dumps(data) if double else data})


def player(name, pid, team, boost=None, **st):
    p = {"Name": name, "PrimaryId": pid, "Shortcut": 1, "TeamNum": team, "Score": 0, "Goals": 0, "Shots": 0,
         "Assists": 0, "Saves": 0, "Touches": 0, "CarTouches": 0, "Demos": 0}
    p.update({k.capitalize() if k != "car_touches" else "CarTouches": v for k, v in st.items()})
    if boost is not None:
        p["Boost"] = boost
    return p


def state(players, blue, orange, playlist=11, time_left=200, guid="GUID1", target=None, overtime=False):
    game = {"Teams": [{"Name": "Blue", "TeamNum": 0, "Score": blue}, {"Name": "Orange", "TeamNum": 1, "Score": orange}],
            "PlaylistId": playlist, "TimeSeconds": time_left, "bOvertime": overtime, "bReplay": False,
            "Arena": "Stadium_P", "bHasTarget": target is not None}
    if target:
        game["Target"] = {"Name": target[0], "Shortcut": 1, "TeamNum": target[1]}
    return msg("UpdateState", {"MatchGuid": guid, "Players": players, "Game": game})


def doubles_match(winner=0, blue=3, orange=1, guid="GUID1", kickoff=True, known=True, time_left=0, target=None):
    """A 2v2 where you (Me) are blue with Mate; Opp1/Opp2 orange. Returns (records, log lines)."""
    out, logs = [], []
    feed = stats.GameFeed(out.append, {stats.strip_split(MY_ID)} if known else set(), logs.append)
    feed.handle(msg("MatchCreated", {"MatchGuid": guid}))
    if kickoff:
        feed.handle(msg("RoundStarted", {"MatchGuid": guid}))
    roster = lambda b1, b2: [
        player("Me", MY_ID, 0, boost=b1, score=420, goals=2, shots=6, saves=1, assists=0, touches=30, demos=1),
        player("Mate", "Steam|76561198000000001|0", 0, score=310, goals=1, shots=2, saves=2, assists=2, touches=22),
        player("Opp1", "Epic|11111111111111111111111111111111|0", 1, score=250, goals=1, shots=3, saves=3, touches=25,
               demos=2),
        player("Opp2", "PS4|22|0", 1, score=120, shots=1, saves=1, touches=12)]
    feed.handle(state(roster(20, None), blue, orange, guid=guid, target=target, time_left=150))
    feed.handle(msg("StatfeedEvent", {"MatchGuid": guid, "EventName": "Demolish", "Type": "Demolition",
                                      "MainTarget": {"Name": "Opp1", "Shortcut": 3, "TeamNum": 1},
                                      "SecondaryTarget": {"Name": "Me", "Shortcut": 1, "TeamNum": 0}}))
    feed.handle(state(roster(60, None), blue, orange, guid=guid, target=target, time_left=time_left))
    feed.handle(msg("MatchEnded", {"MatchGuid": guid, "WinnerTeamNum": winner}))
    feed.handle(msg("MatchDestroyed", {"MatchGuid": guid}))
    return out, logs


class StatsFeedTests(unittest.TestCase):
    def test_decode_double_and_single_encoded(self):
        self.assertEqual(stats.decode(msg("MatchEnded", {"WinnerTeamNum": 1})), ("MatchEnded", {"WinnerTeamNum": 1}))
        self.assertEqual(stats.decode(msg("MatchEnded", {"WinnerTeamNum": 1}, double=False))[1], {"WinnerTeamNum": 1})
        self.assertEqual(stats.decode("not json"), (None, {}))

    def test_full_doubles_match(self):
        out, logs = doubles_match()
        self.assertEqual(len(out), 1, logs)
        g = out[0]
        self.assertEqual((g.match_guid, g.playlist_id, g.arena, g.my_team), ("GUID1", 11, "Stadium_P", 0))
        self.assertEqual((g.won, g.team_goals, g.opp_goals, g.forfeit, g.self_how), (True, 3, 1, False, "account"))
        names = [(p["name"], p["is_self"], p["teammate"], p["team"]) for p in g.players]
        self.assertEqual(names, [("Me", True, False, 0), ("Mate", False, True, 0), ("Opp1", False, False, 1),
                                 ("Opp2", False, False, 1)])
        me_ = g.players[0]
        self.assertEqual((me_["shots"], me_["saves"], me_["demos"], me_["demoed"], me_["boost_avg"]), (6, 1, 1, 1, 40.0))
        self.assertIsNone(g.players[1]["boost_avg"])
        self.assertEqual(g.players[2]["feed"], {"Demolish": 1})

    def test_loss_and_forfeit(self):
        out, _ = doubles_match(winner=1, blue=1, orange=4, time_left=95)
        self.assertEqual((out[0].won, out[0].forfeit), (False, True))

    def test_self_from_target_when_no_account_matches(self):
        out, _ = doubles_match(known=False, target=("Me", 0))
        self.assertEqual((out[0].self_name, out[0].self_how), ("Me", "target"))
        out, _ = doubles_match(known=False)  # only your car sends boost
        self.assertEqual((out[0].self_name, out[0].self_how), ("Me", "boost"))

    def test_not_saved(self):
        out, logs = doubles_match(winner=-1)
        self.assertEqual(out, [])
        self.assertIn("no result", logs[-1])
        out, logs = doubles_match(guid="")
        self.assertEqual(out, [])
        self.assertIn("Offline", logs[-1])

    def test_cancelled_before_kickoff(self):
        out, logs = [], []
        feed = stats.GameFeed(out.append, {stats.strip_split(MY_ID)}, logs.append)
        feed.handle(msg("MatchCreated", {"MatchGuid": "G"}))
        feed.handle(state([player("Me", MY_ID, 0), player("Opp", "Epic|x|0", 1)], 0, 0, guid="G"))
        feed.handle(msg("MatchEnded", {"MatchGuid": "G", "WinnerTeamNum": 0}))
        self.assertEqual(out, [])
        self.assertIn("before kickoff", logs[-1])

    def test_left_before_the_end(self):
        out, logs = [], []
        feed = stats.GameFeed(out.append, {stats.strip_split(MY_ID)}, logs.append)
        feed.handle(msg("MatchCreated", {"MatchGuid": "G"}))
        feed.handle(state([player("Me", MY_ID, 0, touches=3), player("Opp", "Epic|x|0", 1)], 0, 1, guid="G"))
        feed.handle(msg("MatchDestroyed", {"MatchGuid": "G"}))
        self.assertEqual(out, [])
        self.assertIn("Left before", logs[-1])

    def test_spectating_is_not_saved(self):
        out, logs = [], []
        feed = stats.GameFeed(out.append, set(), logs.append)
        feed.handle(msg("RoundStarted", {}))
        feed.handle(state([player("A", "Epic|a|0", 0, boost=10, touches=2), player("B", "Epic|b|0", 1, boost=50)],
                          1, 0, target=("A", 0)))
        feed.handle(msg("MatchEnded", {"WinnerTeamNum": 0}))
        self.assertEqual(out, [])
        self.assertIn("spectating", logs[-1])

    def test_decide_result(self):
        self.assertTrue(stats.decide_result(0, {0: 3, 1: 1}, 0))
        self.assertFalse(stats.decide_result(1, {0: 3, 1: 1}, 0))
        self.assertTrue(stats.decide_result(1, {0: 3, 1: 1}, None) is False)
        self.assertTrue(stats.decide_result(0, {0: 2, 1: 2}, 0))      # level score: the winner decides
        self.assertTrue(stats.decide_result(1, {0: 0, 1: 2}, 0))      # the score wins over a wrong WinnerTeamNum
        self.assertIsNone(stats.decide_result(0, {0: 2, 1: 1}, -1))   # no contest
        self.assertIsNone(stats.decide_result(0, {}, None))

    def test_bots_keep_separate_lines(self):
        out = []
        feed = stats.GameFeed(out.append, {stats.strip_split(MY_ID)}, save_offline=True)
        feed.handle(msg("RoundStarted", {}))
        feed.handle(state([player("Me", MY_ID, 0, touches=5), player("Bot A", "Unknown|0|0", 1, saves=2),
                           player("Bot B", "Unknown|0|0", 1, shots=1)], 1, 0, guid=""))
        feed.handle(msg("MatchEnded", {"WinnerTeamNum": 0}))
        self.assertEqual(len(out[0].players), 3)
        self.assertTrue(out[0].match_guid.startswith("local-"))


class StatsReaderLoadTests(unittest.TestCase):
    """The reader must stay cheap whatever the game sends (FPS dropped in an Honor Duel with RLUtils on)."""

    def test_peek_event(self):
        self.assertEqual(stats.peek_event(msg("BallHit", {})), "BallHit")
        self.assertEqual(stats.peek_event(msg("UpdateState", {}).encode()), "UpdateState")
        self.assertEqual(stats.peek_event({"Event": "MatchEnded", "Data": "{}"}), "MatchEnded")
        self.assertIsNone(stats.peek_event("garbage"))

    def test_unused_events_are_not_decoded(self):
        feed = stats.GameFeed(lambda r: None)
        feed.handle('{"Event": "BallHit", "Data": "{not json at all"}')  # would fail to decode; never tried
        self.assertEqual(feed.counts, {"BallHit": 1})

    def test_states_are_throttled_but_the_last_one_counts(self):
        now = [0.0]
        out = []
        feed = stats.GameFeed(out.append, {stats.strip_split(MY_ID)}, clock=lambda: now[0])
        feed.handle(msg("MatchCreated", {"MatchGuid": "G"}))
        feed.handle(msg("RoundStarted", {}))
        decoded = []
        real = feed._update_state
        feed._update_state = lambda d: (decoded.append(d), real(d))
        for i in range(1, 51):  # 50 states in 0.5 s: a flood
            now[0] = i * 0.01
            feed.handle(state([player("Me", MY_ID, 0, shots=i, touches=i), player("Opp", "Epic|x|0", 1)], 1, 0,
                              guid="G"))
        self.assertLessEqual(len(decoded), 4)
        feed.handle(msg("MatchEnded", {"WinnerTeamNum": 0}))
        self.assertEqual(out[0].players[0]["shots"], 50)  # the pending last state was decoded first

    def test_rate_report(self):
        now, lines = [0.0], []
        feed = stats.GameFeed(lambda r: None, clock=lambda: now[0], on_rate=lines.append)
        for i in range(1001):
            now[0] = i * 0.01  # 100 msg/s for 10 s
            feed.handle(msg("BoostPickup", {}))
        self.assertEqual(len(lines), 1)
        self.assertIn("BoostPickup", lines[0])
        quiet, lines2 = [0.0], []
        feed = stats.GameFeed(lambda r: None, clock=lambda: quiet[0], on_rate=lines2.append)
        for i in range(201):
            quiet[0] = i * 0.1  # 10 msg/s: normal
            feed.handle(msg("UpdateState", {}))
        self.assertEqual(lines2, [])

    def test_bad_message_does_not_stop_the_reader(self):
        events = __import__("queue").Queue()

        class Boom(stats.GameFeed):
            def _handle(self, event, data):
                raise KeyError("x")

        stats._safe_handle(Boom(lambda r: None), msg("MatchEnded", {}), events)
        self.assertEqual(events.get_nowait()[0], "error")

    def test_tcp_stream_split_across_packets(self):
        import socket as so
        import threading as th
        import queue as qu
        server = so.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        blob = (msg("MatchCreated", {"MatchGuid": "G"}) + "\n" + msg("RoundStarted", {}) +
                state([player("Me", MY_ID, 0, touches=4, goals=1), player("Opp", "Epic|x|0", 1)], 1, 0, guid="G") +
                msg("MatchEnded", {"WinnerTeamNum": 0})).encode()

        def serve():
            conn, _ = server.accept()
            for i in range(0, len(blob), 37):  # odd packet sizes cut messages in half
                conn.sendall(blob[i:i + 37])
            conn.close()

        th.Thread(target=serve, daemon=True).start()
        out, events, stop = [], qu.Queue(), th.Event()
        feed = stats.GameFeed(out.append, {stats.strip_split(MY_ID)})
        self.assertTrue(stats._read_tcp(feed, "127.0.0.1", port, stop, events))
        server.close()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].won)
        self.assertEqual(events.get_nowait(), ("status", f"connected (TCP {port})"))


class GamesDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_list_dedupe_delete(self):
        rec = doubles_match()[0][0]
        key = data.profile_for_player(rec.self_id, rec.self_name, self.path)
        self.assertEqual(data.get_profile(key, self.path)["player_id"], "Epic|0a1b2c3d4e5f60718293a4b5c6d7e8f9|0")
        gid = data.save_game(key, rec, self.path)
        self.assertIsNotNone(gid)
        self.assertIsNone(data.save_game(key, rec, self.path))
        games = data.list_games(key, path=self.path)
        self.assertEqual(len(games), 1)
        g = games[0]
        self.assertEqual((g["won"], g["team_goals"], g["opp_goals"], g["playlist_id"]), (1, 3, 1, 11))
        self.assertEqual([p["name"] for p in g["players"]], ["Me", "Mate", "Opp1", "Opp2"])
        self.assertTrue(g["players"][0]["is_self"] and g["players"][1]["teammate"])
        self.assertEqual(g["players"][2]["feed"], {"Demolish": 1})
        self.assertEqual(data.game_playlists(key, self.path), [(11, 1)])
        self.assertEqual(data.list_games(key, 13, self.path), [])
        data.delete_game(gid, self.path)
        self.assertEqual(data.list_games(key, path=self.path), [])

    def test_profile_for_steam_player(self):
        a = data.profile_for_player("Steam|765|0", "Someone", self.path)
        self.assertEqual(data.profile_for_player("Steam|765|1", "Renamed", self.path), a)


def fake_game(won, me_st, mate_st=None, opp_st=None, size=2, pid=11):
    """A saved-game dict as data.list_games returns it."""
    base = dict.fromkeys(("score", "goals", "shots", "assists", "saves", "touches", "demos"), 0)
    players = [{**base, **me_st, "name": "Me", "primary_id": MY_ID, "team": 0, "is_self": True, "teammate": False}]
    for i in range(size - 1):
        players.append({**base, **(mate_st or {}), "name": f"Mate{i}", "primary_id": f"Epic|m{i}|0", "team": 0,
                        "is_self": False, "teammate": True})
    for i in range(size):
        players.append({**base, **(opp_st or {}), "name": f"Opp{i}", "primary_id": f"Epic|o{i}|0", "team": 1,
                        "is_self": False, "teammate": False})
    return {"won": int(won), "playlist_id": pid, "players": players}


class GameAnalysisTests(unittest.TestCase):
    def sample(self):
        """10 wins: you shoot a lot (8 of 10 over 5 shots); 10 losses: you save a lot (8 of 10 over 3 saves)."""
        games = []
        for i in range(10):
            games.append(fake_game(True, {"shots": 6 if i < 8 else 5, "saves": 3 if i < 8 else 4, "score": 400},
                                   {"shots": 2, "saves": 1}, {"shots": 3}))
            games.append(fake_game(False, {"shots": 5 if i < 8 else 6, "saves": 4 if i < 8 else 3, "score": 400},
                                   {"shots": 2, "saves": 1}, {"shots": 3}))
        return games

    def test_patterns_found(self):
        found, note = stats.find_patterns(self.sample())
        texts = [p.text() for p in found]
        self.assertEqual(note, "")
        self.assertIn("80% of wins you have over 5 shots (vs 20% of losses)", texts)
        self.assertIn("80% of losses you have over 3 saves (vs 20% of wins)", texts)
        self.assertNotIn("points", " ".join(texts))  # the same in wins and losses
        self.assertFalse(any("or fewer shots" in t for t in texts))  # the same split as "over 5 shots", said once
        self.assertTrue(all(p.p <= stats.MAX_P for p in found))

    def test_point_thresholds_are_round(self):
        games = [fake_game(True, {"score": 430 + i}) for i in range(8)] + \
                [fake_game(False, {"score": 260 + i}) for i in range(8)]
        texts = [p.text() for p in stats.find_patterns(games)[0]]
        self.assertIn("100% of wins you have over 400 points (vs 0% of losses)", texts)

    def test_patterns_need_enough_games(self):
        found, note = stats.find_patterns(self.sample()[:7])
        self.assertEqual(found, [])
        self.assertIn("at least 5 wins and 5 losses", note)

    def test_no_pattern_without_a_difference(self):
        games = [fake_game(i < 10, {"shots": i % 5, "saves": i % 3}) for i in range(20)]  # same spread both sides
        found, note = stats.find_patterns(games)
        self.assertEqual(found, [])
        self.assertIn("No clear difference", note)

    def test_fisher(self):
        self.assertAlmostEqual(stats.fisher_p(8, 2, 2, 8), 0.0230, places=3)
        self.assertAlmostEqual(stats.fisher_p(5, 5, 5, 5), 1.0)

    def test_game_tags(self):
        g = fake_game(True, {"shots": 6, "saves": 0, "assists": 0, "demos": 3},
                      {"shots": 1, "saves": 3, "assists": 2, "demos": 0}, {"demos": 1})
        self.assertEqual(stats.game_tags(g, g["players"][0]), ["shooter", "demo"])
        self.assertEqual(stats.game_tags(g, g["players"][1]), ["keeper", "playmaker"])
        duel = fake_game(True, {"shots": 5, "saves": 1}, opp_st={"shots": 1, "saves": 4}, size=1)
        self.assertEqual(stats.game_tags(duel, duel["players"][0]), ["shooter"])
        self.assertEqual(stats.game_tags(duel, duel["players"][1]), ["keeper"])

    def test_playstyle_and_teammates(self):
        games = self.sample()
        style = stats.playstyle(games)
        self.assertEqual(style["games"], 20)
        self.assertIn("shooter", style["tags"])
        mates = stats.teammates(games, min_games=3)
        self.assertEqual(mates["games"], 20)
        self.assertEqual(mates["score"]["wins"][2], 10)
        self.assertEqual(mates["regulars"][0]["games"], 20)


def timed_game(gid, minute, won, pid=11, **me_st):
    """fake_game with an id and times: ended at 20:00 + minute on 12 Oct 2026 (UTC), 6 minutes long."""
    g = fake_game(won, me_st, pid=pid)
    end = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    g.update(id=gid, ended_at=end.isoformat(), started_at=(end - timedelta(minutes=6)).isoformat(),
             team_goals=3 if won else 1, opp_goals=1 if won else 3, overtime=0, forfeit=0, arena="")
    return g


class ViewModelTests(unittest.TestCase):
    def test_signs_and_ranks(self):
        self.assertEqual([viewmodel.sign(9), viewmodel.sign(-9), viewmodel.sign(0)], ["+9", "−9", "±0"])
        self.assertEqual(viewmodel.rank_text(15, 0), "Diamond III · Div I")
        self.assertEqual(viewmodel.rank_text(22, None), "Supersonic Legend")
        now = datetime(2026, 10, 20, 22, 0)
        self.assertEqual(viewmodel.when_label(datetime(2026, 10, 20, 21, 48), now), "Today 21:48")
        self.assertEqual(viewmodel.when_label(datetime(2026, 10, 19, 9, 5), now), "Yesterday 09:05")
        self.assertEqual(viewmodel.when_label(datetime(2026, 10, 2, 18, 5), now), "Oct 2 18:05")

    def test_mmr_change_only_when_one_game_between_readings(self):
        base = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
        games = [timed_game(1, 0, True), timed_game(2, 30, True), timed_game(3, 40, False)]
        snaps = {11: [(base - timedelta(minutes=10), 1000), (base + timedelta(minutes=5), 1009),
                      (base + timedelta(minutes=45), 1010)]}
        self.assertEqual(viewmodel.game_mmr_deltas(games, snaps), {1: 9})  # games 2 and 3 share one reading
        late = {11: [(base - timedelta(minutes=10), 1000), (base + timedelta(hours=5), 1009)]}
        self.assertEqual(viewmodel.game_mmr_deltas(games[:1], late), {})  # the next reading is far too late

    def test_sessions(self):
        games = [timed_game(1, 0, True), timed_game(2, 10, True), timed_game(3, 120, False), timed_game(4, 128, True)]
        pos = viewmodel.session_positions(games, timedelta(minutes=30))
        self.assertEqual(pos, {1: 1, 2: 2, 3: 1, 4: 2})
        now = datetime(2026, 10, 12, 22, 20, tzinfo=timezone.utc)
        n, length = viewmodel.current_session(games, timedelta(minutes=30), now)
        self.assertEqual((n, length), (2, timedelta(minutes=14)))
        self.assertEqual(viewmodel.current_session(games, timedelta(minutes=30), now + timedelta(hours=2))[0], 0)

    def test_games_page(self):
        games = [timed_game(i, -i * 10, i % 3 != 0, goals=1 if i % 2 else 0, shots=2, saves=1) for i in range(1, 9)]
        vm = viewmodel.games_page(games, None, None, {1: 9}, {g["id"]: 1 for g in games},
                                  datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(vm["rec"]["wl"], "6–2")
        self.assertEqual(vm["rec"]["streak"], "W2")
        self.assertEqual(vm["rec"]["mmr"], "+9 MMR")
        self.assertEqual(vm["sel"], 1)
        self.assertEqual(vm["list"][0]["delta"], "+9")
        self.assertEqual(vm["list"][1]["delta"], "")
        self.assertEqual(vm["mates"]["best"], "Mate0")
        self.assertEqual([g["label"] for g in vm["detail"]["groups"]], ["You", "Teammates", "Opponents"])
        mvp = [r["name"] for g in vm["detail"]["groups"] for r in g["rows"] if r["mvp"]]
        self.assertEqual(len(mvp), 1)
        self.assertEqual(vm["hyp"]["patterns"][0]["label"], "After a win")
        none = viewmodel.games_page(games, 13, None, {}, {}, datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc))
        self.assertIsNone(none["pl"])  # no 3v3 games: back to all

    def test_bots_and_ranked_casual(self):
        games = [timed_game(1, 0, True), timed_game(2, -10, False, pid=2), timed_game(3, -20, True, pid=2)]
        for g in games[1:]:  # casual: the teammate left and a bot took the slot
            g["players"][1]["left_early"] = True
            g["players"].append({**g["players"][1], "name": "Wolfman", "primary_id": "Unknown|0|0",
                                 "left_early": False})
        now = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc)
        vm = viewmodel.games_page(games, None, 2, {}, {}, now)
        self.assertEqual([m["name"] for m in vm["hyp"]["mates"]], ["Mate0"])  # the bot isn't a teammate
        self.assertEqual(vm["hyp"]["mates"][0]["gp"], 3)
        self.assertNotIn("Wolfman", vm["list"][1]["with"])
        names = [r["name"] for g in vm["detail"]["groups"] for r in g["rows"]]
        self.assertIn("Wolfman (bot)", names)
        ranked = viewmodel.games_page(games, None, None, {}, {}, now, "Ranked")
        casual = viewmodel.games_page(games, None, None, {}, {}, now, "Casual")
        self.assertEqual(([g["id"] for g in ranked["list"]], [g["id"] for g in casual["list"]]), ([1], [2, 3]))

    def test_chart_bands(self):
        table = [(15, 0, 980, 1000), (15, 1, 1001, 1020), (15, 2, 1021, 1040), (15, 3, 1041, 1060),
                 (16, 0, 1061, 1100)]
        t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
        rows = [(t0 + timedelta(days=i), {"rating": r, "tier_id": None, "division_id": None})
                for i, r in enumerate((1000, 1030, 1070))]
        c = viewmodel.chart(rows, table, "raw")
        self.assertEqual([p["v"] for p in c["points"]], [1000, 1030, 1070])
        self.assertEqual(c["points"][1]["d"], "+30")
        self.assertEqual(c["points"][2]["rank"], "Champion I · Div I")
        self.assertEqual([b["label"] for b in c["bands"]], ["Diamond III", "Champion I"])
        self.assertIn("Div II · 1001", [d["label"] for d in c["divs"]])
        g = viewmodel.gauge(1030, 15, 2, data.table_distance(1030, table))
        self.assertEqual((g["tier"], g["div"], g["toNext"]), ("Diamond III", "Division III", "11 MMR to Div IV"))
        g = viewmodel.gauge(1030, 15, 2, data.tier_distance(1030, 15, [], table))  # what the MMR page shows
        self.assertEqual((g["toNext"], g["pct"]), ("31 MMR to Champion I", "62%"))


# ====================================================================================================
# Setup and launching (0.11): config migration, the built-in switcher, Documents, the Stats API setting,
# finding the game and starting it
# ====================================================================================================
from rlutils import game


def fresh_cfg():
    import copy
    return copy.deepcopy(data.DEFAULTS)


class MigrateConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.switcher, self.shortcut = d / "EpicSwitcher.ps1", d / "Rocket League.exe"

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_install_keeps_defaults_and_runs_once(self):
        cfg = fresh_cfg()
        self.assertTrue(data.migrate_config(cfg))   # just marked as done
        self.assertEqual((cfg["launch"]["switcher"], cfg["launch"]["method"], cfg["launch"]["migrated"]),
                         ("", "auto", True))
        cfg["launch"]["rl_shortcut"] = r"C:\Games\RL.lnk"   # set later with method auto: left alone
        self.assertFalse(data.migrate_config(cfg))
        self.assertEqual(cfg["launch"]["method"], "auto")

    def test_hand_set_shortcut_becomes_path_method_and_bad_method_is_fixed(self):
        cfg = fresh_cfg()
        cfg["launch"]["rl_shortcut"] = r"C:\Games\RL.lnk"
        data.migrate_config(cfg)
        self.assertEqual(cfg["launch"]["method"], "path")
        cfg["launch"]["method"] = "nonsense"
        self.assertTrue(data.migrate_config(cfg))
        self.assertEqual(cfg["launch"]["method"], "auto")

    def test_switcher_and_shortcut_paths(self):
        cfg = fresh_cfg()
        self.assertEqual(data.switcher_path(cfg), data.BUNDLED_SWITCHER)
        self.assertEqual(data.switcher_store(cfg), data.SWITCHER_STORE)
        self.assertIsNone(data.rl_shortcut_path(cfg))
        cfg["launch"].update(switcher=r"D:\mine\EpicSwitcher.ps1", rl_shortcut='"C:\\Games\\RL.lnk"')
        self.assertEqual(data.switcher_path(cfg), Path(r"D:\mine\EpicSwitcher.ps1"))
        self.assertIsNone(data.switcher_store(cfg))   # the user's own script keeps its own accounts
        self.assertEqual(data.rl_shortcut_path(cfg), Path(r"C:\Games\RL.lnk"))

    def test_bundled_switcher_ships_and_has_store_option(self):
        text = data.BUNDLED_SWITCHER.read_text(encoding="utf-8")
        self.assertIn("[string]$StoreDir = ''", text)
        self.assertIn("-ListJson", text)

    @unittest.skipUnless(sys.platform == "win32", "PowerShell")
    def test_bundled_switcher_lists_an_empty_store(self):
        store = Path(self._tmp.name) / "store"
        self.assertEqual(game.switcher_accounts(data.BUNDLED_SWITCHER, store), [])
        self.assertTrue(store.is_dir())


class SetupProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_name_from_setup_gets_the_account_when_it_plays(self):
        data.upsert_profile("Solo Tester", None, self.db)
        self.assertEqual(data.profile_for_epic_account(ACC, "solo tester", self.db), "Solo Tester")
        self.assertEqual(data.get_profile("Solo Tester", self.db)["player_id"], f"Epic|{ACC}|0")
        self.assertEqual(len(data.list_profiles(self.db)), 1)

    def test_profile_with_another_account_is_not_taken(self):
        data.upsert_profile("Solo Tester", f"Epic|{OTHER}|0", self.db)
        self.assertEqual(data.profile_for_epic_account(ACC, "Solo Tester", self.db), f"Solo Tester ({ACC[:6]})")


class DocumentsTests(unittest.TestCase):
    def test_candidates_in_order_without_repeats(self):
        env = {"OneDrive": r"C:\U\OneDrive", "OneDriveConsumer": r"C:\U\OneDrive", "USERPROFILE": r"C:\U"}
        self.assertEqual(rllog.documents_candidates(env, known=Path(r"C:\U\Documents")),
                         [Path(r"C:\U\Documents"), Path(r"C:\U\OneDrive\Documents")])

    def test_first_candidate_with_rocket_league_wins(self):
        with tempfile.TemporaryDirectory() as d:
            old, moved = Path(d) / "Documents", Path(d) / "OneDrive" / "Documents"
            (moved / "My Games" / "Rocket League").mkdir(parents=True)
            old.mkdir()
            self.assertEqual(rllog.rocket_league_dir([old, moved]), moved / "My Games" / "Rocket League")
            self.assertEqual(rllog.rocket_league_dir([old]), old / "My Games" / "Rocket League")  # none: first

    def test_signed_in_players_newest_first(self):
        with tempfile.TemporaryDirectory() as d:
            import os
            old, new = Path(d) / "Launch-backup-1.log", Path(d) / "Launch.log"
            old.write_text("Log: Log file open, 1\n" + me("Old Name", OTHER), encoding="utf-8")
            new.write_text("Log: Log file open, 2\n" + me() + me(), encoding="utf-8")
            os.utime(old, (1, 1))
            self.assertEqual(rllog.signed_in_players(Path(d)), [(ACC, "Solo Tester"), (OTHER, "Old Name")])


class StatsApiSettingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ini = Path(self._tmp.name) / "TAGame" / "Config" / "TAStatsAPI.ini"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_off_and_enable_creates_it(self):
        self.assertEqual(stats.read_stats_api(self.ini)["exists"], False)
        stats.enable_stats_api(self.ini)
        self.assertEqual(self.ini.read_bytes(), b"[TAGame.MatchStatsExporter_TA]\r\nPort=49123\r\nPacketSendRate=10\r\n")
        state = stats.read_stats_api(self.ini)
        self.assertEqual((state["enabled"], state["rate"], state["port"]), (True, 10, 49123))

    def test_off_file_is_switched_on_and_rest_kept(self):
        self.ini.parent.mkdir(parents=True)
        before = "[TAGame.MatchStatsExporter_TA]\nPort=49200\nPacketSendRate=0\n\n[IniVersion]\n0=1791386884.000000\n"
        self.ini.write_text(before, encoding="utf-8", newline="")
        self.assertFalse(stats.read_stats_api(self.ini)["enabled"])
        stats.enable_stats_api(self.ini)
        after = self.ini.read_text(encoding="utf-8", newline="")
        self.assertEqual(after, before.replace("PacketSendRate=0", "PacketSendRate=10"))   # \n endings kept
        self.assertEqual(stats.read_stats_api(self.ini)["port"], 49200)
        self.assertEqual(self.ini.with_name("TAStatsAPI.ini.bak").read_text(encoding="utf-8"), before)

    def test_section_or_keys_missing_are_added(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[IniVersion]\r\n0=1\r\n", encoding="utf-8", newline="")
        stats.enable_stats_api(self.ini)
        self.assertEqual(self.ini.read_text(encoding="utf-8", newline=""),
                         "[TAGame.MatchStatsExporter_TA]\r\nPort=49123\r\nPacketSendRate=10\r\n\r\n[IniVersion]\r\n0=1\r\n")
        self.ini.write_text("[TAGame.MatchStatsExporter_TA]\r\n[Other]\r\nPacketSendRate=0\r\n", encoding="utf-8", newline="")
        stats.enable_stats_api(self.ini)
        text = self.ini.read_text(encoding="utf-8", newline="")
        self.assertEqual(text, "[TAGame.MatchStatsExporter_TA]\r\nPort=49123\r\nPacketSendRate=10\r\n[Other]\r\nPacketSendRate=0\r\n")

    def test_utf16_file_stays_utf16(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[TAGame.MatchStatsExporter_TA]\r\nPacketSendRate=0\r\n", encoding="utf-16", newline="")
        stats.enable_stats_api(self.ini)
        self.assertTrue(self.ini.read_bytes().startswith(b"\xff\xfe"))
        self.assertTrue(stats.read_stats_api(self.ini)["enabled"])

    def test_install_file_name(self):
        config = Path(self._tmp.name) / "rl" / "TAGame" / "Config"
        config.mkdir(parents=True)
        self.assertEqual(stats.install_ini_path(config.parent.parent), config / "DefaultStatsAPI.ini")
        (config / "TAStatsAPI.ini").write_text("x")
        self.assertEqual(stats.install_ini_path(config.parent.parent), config / "TAStatsAPI.ini")

    def test_state_prefers_the_documents_copy(self):
        inst = Path(self._tmp.name) / "DefaultStatsAPI.ini"
        inst.write_text("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=10\n", encoding="utf-8")
        state = stats.stats_api_state(self.ini, inst)       # no Documents copy yet: the game folder's decides
        self.assertEqual((state["enabled"], state["install_on"]), (True, True))
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=0\n", encoding="utf-8")
        state = stats.stats_api_state(self.ini, inst)       # the game reads the Documents copy
        self.assertEqual((state["enabled"], state["install_on"]), (False, True))
        self.assertIsNone(stats.stats_api_state(self.ini)["install_on"])

    def test_enable_everywhere_and_unwritable_game_folder(self):
        inst = Path(self._tmp.name) / "DefaultStatsAPI.ini"
        inst.write_text("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=0\n", encoding="utf-8")
        changed, problems = stats.enable_stats_everywhere(self.ini, inst)
        self.assertEqual((sorted(changed), problems), (sorted([str(inst), str(self.ini)]), []))
        self.assertEqual(stats.enable_stats_everywhere(self.ini, inst), ([], []))   # already on: untouched
        blocked = Path(self._tmp.name) / "blocked.ini"
        blocked.mkdir()   # a folder where the file should be: writing fails like a read-only game folder
        self.ini.unlink()
        changed, problems = stats.enable_stats_everywhere(self.ini, blocked)
        self.assertEqual(changed, [str(self.ini)])
        self.assertTrue(problems and problems[0].startswith(str(blocked)))

    def test_junk_values_read_as_off(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=fast\nPort=x\n", encoding="utf-8")
        state = stats.read_stats_api(self.ini)
        self.assertEqual((state["enabled"], state["port"]), (False, 49123))


class FindGameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_epic_manifest(self):
        manifests = self.root / "Manifests"
        manifests.mkdir()
        install = self.root / "Epic Games" / "rocketleague"
        (install / "Binaries" / "Win64").mkdir(parents=True)
        (install / "Binaries" / "Win64" / "RocketLeague.exe").write_text("x")
        (manifests / "A.item").write_text(json.dumps({"AppName": "Fortnite", "InstallLocation": str(self.root)}))
        (manifests / "B.item").write_text("{broken")
        (manifests / "C.item").write_text(json.dumps({"AppName": "Sugar", "DisplayName": "Rocket League®",
                                                      "InstallLocation": str(install)}), encoding="utf-8")
        found = game.find_epic(manifests)
        self.assertEqual((found["method"], found["dir"]), ("epic", str(install)))
        self.assertTrue(found["exe"].endswith("RocketLeague.exe"))
        self.assertIsNone(game.find_epic(self.root / "nothing"))

    def test_steam_second_library(self):
        steam, lib = self.root / "Steam", self.root / "Games" / "SteamLibrary"
        (steam / "steamapps").mkdir(parents=True)
        (lib / "steamapps" / "common" / "rocketleague").mkdir(parents=True)
        (steam / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(steam).replace("\\", "\\\\") + '"\n\t}\n'
            '\t"1"\n\t{\n\t\t"path"\t\t"' + str(lib).replace("\\", "\\\\") + '"\n\t\t"apps"\n\t\t{\n\t\t\t"252950"\t\t"1"\n\t\t}\n\t}\n}\n')
        (lib / "steamapps" / "appmanifest_252950.acf").write_text('"AppState"\n{\n\t"appid"\t\t"252950"\n\t"installdir"\t\t"rocketleague"\n}\n')
        found = game.find_steam(steam)
        self.assertEqual((found["method"], found["dir"], found["exe"]),
                         ("steam", str(lib / "steamapps" / "common" / "rocketleague"), ""))
        self.assertIsNone(game.find_steam(self.root / "nosteam"))

    def test_resolve_launch(self):
        epic = {"method": "epic", "dir": "x", "exe": ""}
        steam = {"method": "steam", "dir": "y", "exe": ""}
        self.assertEqual(game.resolve_launch("auto", None, [steam, epic]), ("uri", game.EPIC_LAUNCH))
        self.assertEqual(game.resolve_launch("auto", None, [steam]), ("uri", game.STEAM_LAUNCH))
        self.assertEqual(game.resolve_launch("steam", None, [epic, steam]), ("uri", game.STEAM_LAUNCH))
        for method, installs in (("auto", []), ("epic", [steam]), ("steam", [epic]), ("path", [epic])):
            with self.assertRaises(game.ReadError):
                game.resolve_launch(method, None, installs)
        lnk = self.root / "Rocket League.lnk"
        lnk.write_text("x")
        self.assertEqual(game.resolve_launch("path", lnk, []), ("file", str(lnk)))

    def test_check_game_file(self):
        txt, exe = self.root / "notes.txt", self.root / "RocketLeague.exe"
        txt.write_text("x")
        self.assertIn("not a .txt", game.check_game_file(txt))
        self.assertIn("Not found", game.check_game_file(exe))
        exe.write_text("x")
        self.assertEqual(game.check_game_file(exe), "")


if __name__ == "__main__":
    unittest.main()
