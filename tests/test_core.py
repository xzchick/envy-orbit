"""Core Orbit tests: encrypted orbits, tracks, the completion log and its hash
chain, cadence classification (pending/on-track/soon/due/overdue), revision-
preserving saves, export and the no-overwrite guarantee.
"""

import datetime
import json
from pathlib import Path
import tempfile
import unittest

from envy_orbit import core
from envy_orbit.core import OrbitError

UTC = datetime.timezone.utc


def when(base, days):
    return (base + datetime.timedelta(days=days)).isoformat()


class OrbitTest(unittest.TestCase):
    SECRET = b"a long private orbit passphrase"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="envy-orbit-"))
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self.base = datetime.datetime(2026, 1, 1, tzinfo=UTC)

    def orbit(self, name="o.envyorbit", title="Maintenance"):
        path = self.dir / name
        core.create(path, title, self.SECRET)
        return path

    def _seq(self):
        """A counter so each save lands in its own file (revisions are preserved)."""
        self._n = getattr(self, "_n", 0) + 1
        return self.dir / f"o.r{self._n}.envyorbit"

    # ----- envelope / create -------------------------------------------
    def test_encrypted_round_trip_and_magic(self):
        blob = core.encrypt_bytes(b"x", self.SECRET)
        self.assertEqual(blob[:11], b"ENVYVAULT2\x00")
        self.assertEqual(core.decrypt_bytes(blob, self.SECRET), b"x")

    def test_create_empty_orbit(self):
        path = self.orbit(title="Key rotation")
        data, _ = core._load(path, self.SECRET)
        self.assertEqual(data["revision"], 0)
        self.assertEqual(data["title"], "Key rotation")
        self.assertEqual(data["tracks"], [])
        self.assertEqual(data["journal"], [])

    def test_short_passphrase_rejected(self):
        with self.assertRaises(OrbitError):
            core.create(self.dir / "s", "t", b"short")

    # ----- tracks ------------------------------------------------------
    def test_add_track_preserves_previous_revision(self):
        p0 = self.orbit()
        p1 = self._seq()
        res = core.add_track(p0, p1, self.SECRET, "Rotate signing key", 90, grace_days=7)
        self.assertEqual(res["revision"], 1)
        self.assertTrue(p0.exists(), "previous revision preserved")
        data, _ = core._load(p1, self.SECRET)
        self.assertEqual(len(data["tracks"]), 1)
        t = data["tracks"][0]
        self.assertEqual((t["title"], t["cadence_days"], t["grace_days"]), ("Rotate signing key", 90, 7))

    def test_cadence_bounds_enforced(self):
        p0 = self.orbit()
        with self.assertRaises(OrbitError):
            core.add_track(p0, self._seq(), self.SECRET, "Too often", 0)
        with self.assertRaises(OrbitError):
            core.add_track(p0, self._seq(), self.SECRET, "Too rare", 99999)

    def test_remove_track_keeps_journal_history(self):
        p0 = self.orbit()
        p1 = self._seq()
        tid = core.add_track(p0, p1, self.SECRET, "Backup check", 7)["track_id"]
        p2 = self._seq()
        core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0), note="verified")
        p3 = self._seq()
        core.remove_track(p2, p3, self.SECRET, tid)
        data, _ = core._load(p3, self.SECRET)
        self.assertEqual(data["tracks"], [])
        self.assertEqual(len(data["journal"]), 1, "append-only log is preserved")

    def test_remove_unknown_track_errors(self):
        p0 = self.orbit()
        with self.assertRaises(OrbitError):
            core.remove_track(p0, self._seq(), self.SECRET, "11111111-1111-1111-1111-111111111111")

    # ----- logging / journal chain -------------------------------------
    def test_log_chains_and_rejects_unknown_track(self):
        p0 = self.orbit()
        p1 = self._seq()
        tid = core.add_track(p0, p1, self.SECRET, "Review policy", 30)["track_id"]
        p2 = self._seq()
        core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0))
        p3 = self._seq()
        core.log(p2, p3, self.SECRET, tid, done_utc=when(self.base, 30))
        data, _ = core._load(p3, self.SECRET)
        self.assertEqual(len(data["journal"]), 2)
        self.assertEqual(data["journal"][1]["previous_sha256"], data["journal"][0]["record_sha256"])
        with self.assertRaises(OrbitError):
            core.log(p3, self._seq(), self.SECRET, "22222222-2222-2222-2222-222222222222")

    def test_tampered_journal_breaks_chain(self):
        p0 = self.orbit()
        p1 = self._seq()
        tid = core.add_track(p0, p1, self.SECRET, "Audit", 30)["track_id"]
        p2 = self._seq()
        core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0), note="first")
        data, _ = core._load(p2, self.SECRET)
        data["journal"][0]["note"] = "forged"          # tamper without re-hashing
        forged = self.dir / "forged.envyorbit"
        forged.write_bytes(core.encrypt_bytes(core._canonical(data), self.SECRET))
        with self.assertRaises(OrbitError):
            core.inspect(forged, self.SECRET)

    # ----- status classification ---------------------------------------
    def _one_track(self, cadence, grace, done_offset):
        p0 = self.orbit()
        p1 = self._seq()
        tid = core.add_track(p0, p1, self.SECRET, "Track", cadence, grace_days=grace)["track_id"]
        p2 = self._seq()
        core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, done_offset))
        return p2

    def test_status_states_across_the_cadence(self):
        # cadence 30, grace 3, last done at day 0 -> next due day 30.
        path = self._one_track(30, 3, 0)

        def state_at(day):
            rep = core.status(path, self.SECRET, now=when(self.base, day), lookahead=7)
            return rep["tracks"][0]["state"]

        self.assertEqual(state_at(10), "on-track")   # 20 days out, beyond the 7-day lookahead
        self.assertEqual(state_at(25), "soon")       # 5 days out, within lookahead
        self.assertEqual(state_at(31), "due")        # past due, inside the 3-day grace
        self.assertEqual(state_at(34), "overdue")    # past due + grace

    def test_never_logged_track_is_pending(self):
        p0 = self.orbit()
        p1 = self._seq()
        core.add_track(p0, p1, self.SECRET, "Fresh track", 14)
        rep = core.status(p1, self.SECRET, now=when(self.base, 100))
        self.assertEqual(rep["tracks"][0]["state"], "pending")
        self.assertIsNone(rep["tracks"][0]["next_due_utc"])
        self.assertEqual(rep["needs_attention"], 1)

    def test_status_orders_most_urgent_first_and_counts(self):
        p0 = self.orbit()
        p = p0
        # An overdue track (cadence 7, done 30 days ago) and an on-track one (cadence 365, done today).
        nxt = self._seq(); tid_over = core.add_track(p, nxt, self.SECRET, "Overdue", 7)["track_id"]; p = nxt
        nxt = self._seq(); core.log(p, nxt, self.SECRET, tid_over, done_utc=when(self.base, -30)); p = nxt
        nxt = self._seq(); tid_ok = core.add_track(p, nxt, self.SECRET, "Fine", 365)["track_id"]; p = nxt
        nxt = self._seq(); core.log(p, nxt, self.SECRET, tid_ok, done_utc=when(self.base, 0)); p = nxt
        rep = core.status(p, self.SECRET, now=when(self.base, 0), lookahead=7)
        self.assertEqual(rep["tracks"][0]["title"], "Overdue")
        self.assertEqual(rep["tracks"][0]["state"], "overdue")
        self.assertEqual(rep["counts"]["overdue"], 1)
        self.assertEqual(rep["counts"]["on-track"], 1)
        self.assertFalse(rep["all_clear"])

    def test_latest_completion_drives_next_due(self):
        p0 = self.orbit()
        p1 = self._seq()
        tid = core.add_track(p0, p1, self.SECRET, "Rotate", 10)["track_id"]
        p2 = self._seq(); core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0))
        p3 = self._seq(); core.log(p2, p3, self.SECRET, tid, done_utc=when(self.base, 8))
        # Latest completion day 8 -> next due day 18; at day 12 that is on-track, not overdue.
        rep = core.status(p3, self.SECRET, now=when(self.base, 12), lookahead=2)
        self.assertEqual(rep["tracks"][0]["state"], "on-track")
        self.assertEqual(rep["tracks"][0]["completions"], 2)

    # ----- export -------------------------------------------------------
    def test_export_html_and_json(self):
        path = self._one_track(30, 3, -40)   # done 40 days ago -> overdue
        html_out = self.dir / "r.html"
        res = core.export(path, html_out, self.SECRET, "html", now=when(self.base, 0))
        text = html_out.read_text()
        self.assertIn("Content-Security-Policy", text)
        self.assertIn("overdue", text)
        self.assertFalse(res["all_clear"])
        json_out = self.dir / "r.json"
        core.export(path, json_out, self.SECRET, "json", now=when(self.base, 0))
        report = json.loads(json_out.read_text())
        self.assertIn("limitations", report)
        self.assertEqual(report["tracks"][0]["state"], "overdue")

    def test_export_escapes_markup_in_titles(self):
        p0 = self.orbit()
        p1 = self._seq()
        core.add_track(p0, p1, self.SECRET, "Rotate <script> key", 30)
        out = self.dir / "r.html"
        core.export(p1, out, self.SECRET, "html", now=when(self.base, 0))
        body = out.read_text()
        self.assertNotIn("<script>", body.split("<style>")[1])
        self.assertIn("&lt;script&gt;", body)

    # ----- no overwrite -------------------------------------------------
    def test_new_file_never_overwrites(self):
        keep = self.dir / "keep"
        keep.write_bytes(b"precious")
        with self.assertRaises(OrbitError):
            core.new_file(keep, b"clobber")
        self.assertEqual(keep.read_bytes(), b"precious")

    def test_add_track_refuses_to_overwrite(self):
        p0 = self.orbit()
        dest = self.dir / "exists.envyorbit"; dest.write_bytes(b"keep me")
        with self.assertRaises(OrbitError):
            core.add_track(p0, dest, self.SECRET, "x", 30)
        self.assertEqual(dest.read_bytes(), b"keep me")

    # ----- iCal export --------------------------------------------------
    def test_ical_export_writes_due_dates(self):
        p0 = self.orbit(title="Maintenance")
        p1 = self._seq()
        tid = core.add_track(p0, p1, self.SECRET, "Rotate key", 30)["track_id"]
        p2 = self._seq()
        core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0))
        # Evaluate a week later: next due is 2026-01-31.
        out = self.dir / "orbit.ics"
        result = core.write_ical(p2, out, self.SECRET, now=when(self.base, 7))
        text = out.read_bytes().decode("utf-8")  # keep CRLF; read_text() would normalize it
        self.assertEqual(result["events"], 1)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("DTSTART:20260131T000000Z", text)
        self.assertIn("SUMMARY:Rotate key (every 30d)", text)
        self.assertTrue(text.rstrip().endswith("END:VCALENDAR"))

    def test_ical_skips_tracks_never_logged(self):
        p0 = self.orbit()
        p1 = self._seq()
        core.add_track(p0, p1, self.SECRET, "Never done", 30)  # pending, no due date
        out = self.dir / "empty.ics"
        result = core.write_ical(p1, out, self.SECRET)
        self.assertEqual(result["events"], 0)
        self.assertNotIn("BEGIN:VEVENT", out.read_text())

    def test_ical_refuses_to_overwrite(self):
        p0 = self.orbit()
        dest = self.dir / "exists.ics"; dest.write_bytes(b"keep me")
        with self.assertRaises(OrbitError):
            core.write_ical(p0, dest, self.SECRET)
        self.assertEqual(dest.read_bytes(), b"keep me")

    # ----- snooze (v0.2) -----------------------------------------------
    def test_snooze_defers_the_due_date(self):
        p0 = self.orbit()
        p1 = self._seq(); tid = core.add_track(p0, p1, self.SECRET, "Rotate", 7)["track_id"]
        p2 = self._seq(); core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0))
        # Day 9: naturally overdue (cadence 7, logged day 0).
        self.assertIn(core.status(p2, self.SECRET, now=when(self.base, 9))["tracks"][0]["state"],
                      ("due", "overdue"))
        p3 = self._seq(); res = core.snooze(p2, p3, self.SECRET, tid, until=when(self.base, 20))
        self.assertTrue(res["snoozed_until"])
        row = core.status(p3, self.SECRET, now=when(self.base, 9))["tracks"][0]
        self.assertTrue(row["snoozed"])
        self.assertNotIn(row["state"], ("due", "overdue"))
        self.assertTrue(row["next_due_utc"].startswith("2026-01-21"))

    def test_snooze_never_pulls_due_earlier(self):
        p0 = self.orbit()
        p1 = self._seq(); tid = core.add_track(p0, p1, self.SECRET, "Audit", 30)["track_id"]
        p2 = self._seq(); core.log(p1, p2, self.SECRET, tid, done_utc=when(self.base, 0))
        # Snoozing to day 5 (before the natural day-30 due) has no effect.
        p3 = self._seq(); core.snooze(p2, p3, self.SECRET, tid, until=when(self.base, 5))
        row = core.status(p3, self.SECRET, now=when(self.base, 2))["tracks"][0]
        self.assertFalse(row["snoozed"])
        self.assertTrue(row["next_due_utc"].startswith("2026-01-31"))

    def test_snooze_days_then_clear(self):
        p0 = self.orbit()
        p1 = self._seq(); tid = core.add_track(p0, p1, self.SECRET, "Backup", 7)["track_id"]
        p2 = self._seq(); core.snooze(p1, p2, self.SECRET, tid, days=10)
        self.assertIsNotNone(core._load(p2, self.SECRET)[0]["tracks"][0]["snoozed_until"])
        p3 = self._seq(); core.snooze(p2, p3, self.SECRET, tid)   # clear
        self.assertIsNone(core._load(p3, self.SECRET)[0]["tracks"][0]["snoozed_until"])

    def test_snooze_rejects_unknown_track(self):
        p0 = self.orbit()
        with self.assertRaises(OrbitError):
            core.snooze(p0, self._seq(), self.SECRET, "11111111-1111-1111-1111-111111111111", days=3)

    def test_pre_snooze_orbit_migrates_on_load(self):
        p0 = self.orbit()
        p1 = self._seq(); core.add_track(p0, p1, self.SECRET, "x", 7)
        data, _ = core._load(p1, self.SECRET)
        del data["tracks"][0]["snoozed_until"]               # simulate a pre-v0.2 track
        legacy = self.dir / "legacy.envyorbit"
        core.new_file(legacy, core.encrypt_bytes(core._canonical(data), self.SECRET))
        reloaded, _ = core._load(legacy, self.SECRET)        # must not raise
        self.assertIsNone(reloaded["tracks"][0]["snoozed_until"])


if __name__ == "__main__":
    unittest.main()
