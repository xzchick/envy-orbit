"""CLI tests that need no display. The passphrase prompt is patched."""

import contextlib
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from envy_orbit import cli

UTC = datetime.timezone.utc


class CLITest(unittest.TestCase):
    SECRET = b"a long private orbit passphrase"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="envy-orbit-cli-"))
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self._n = 0

    def seq(self):
        self._n += 1
        return self.dir / f"o.r{self._n}.envyorbit"

    def invoke(self, *args, secret=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_passphrase", return_value=secret or self.SECRET):
            code = cli.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("envy-orbit", out.getvalue())

    def test_init_add_log_status_flow(self):
        base = self.dir / "o.envyorbit"
        code, out, _ = self.invoke("init", base, "--title", "Maintenance")
        self.assertEqual(code, 0)
        self.assertTrue(base.exists())

        # Add a track; capture its id from the output.
        p1 = self.seq()
        code, out, _ = self.invoke("add", base, p1, "--title", "Rotate key", "--every", "30", "--grace", "3")
        self.assertEqual(code, 0)
        tid = out.split("track added ")[1].split(" ")[0]

        # Never logged -> pending -> needs attention (exit 1).
        code, out, _ = self.invoke("status", p1)
        self.assertEqual(code, 1)
        self.assertIn("need attention", out)

        # Log a completion now -> on-track -> all clear (exit 0).
        p2 = self.seq()
        code, out, _ = self.invoke("log", p1, p2, "--track", tid, "--note", "done")
        self.assertEqual(code, 0)
        code, out, _ = self.invoke("status", p2)
        self.assertEqual(code, 0)
        self.assertIn("ALL CLEAR", out)

    def test_inspect_and_export(self):
        base = self.dir / "o.envyorbit"
        self.invoke("init", base, "--title", "Proj")
        p1 = self.seq()
        self.invoke("add", base, p1, "--title", "Backup check", "--every", "7")
        code, out, _ = self.invoke("inspect", p1)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["tracks"][0]["title"], "Backup check")
        out_html = self.dir / "r.html"
        code, out, _ = self.invoke("export", p1, out_html, "--format", "html")
        self.assertEqual(code, 0)
        self.assertIn("sha256", out)

    def test_log_at_specific_time_detects_overdue(self):
        base = self.dir / "o.envyorbit"
        self.invoke("init", base, "--title", "Proj")
        p1 = self.seq()
        _, out, _ = self.invoke("add", base, p1, "--title", "Rotate", "--every", "7")
        tid = out.split("track added ")[1].split(" ")[0]
        p2 = self.seq()
        long_ago = (datetime.datetime.now(UTC) - datetime.timedelta(days=30)).isoformat()
        self.invoke("log", p1, p2, "--track", tid, "--at", long_ago)
        code, out, _ = self.invoke("status", p2)
        self.assertEqual(code, 1)
        self.assertIn("overdue", out)

    def test_bad_passphrase_is_error(self):
        base = self.dir / "o.envyorbit"
        self.invoke("init", base, "--title", "Proj")
        code, _, err = self.invoke("status", base, secret=b"the wrong passphrase!")
        self.assertEqual(code, 2)
        self.assertIn("passphrase", err.lower())

    def test_unknown_command(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["seance"])


if __name__ == "__main__":
    unittest.main()
