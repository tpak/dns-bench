from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from dnsbench import paths

ROOT = Path(__file__).resolve().parents[1]


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.elsewhere = Path(self.tmp.name)  # like site-packages: no checkout markers

    def tearDown(self):
        self.tmp.cleanup()

    def test_this_repo_is_a_checkout(self):
        self.assertEqual(paths.CHECKOUT_DIR, ROOT)
        self.assertTrue(paths.in_checkout())
        self.assertFalse(paths.in_checkout(self.elsewhere))
        self.assertEqual(paths.WEB_DIR, ROOT / "dnsbench" / "web")

    def test_data_lives_in_the_checkout_by_default(self):
        self.assertEqual(paths.resolve(environ={}), paths.DataPaths(ROOT / "config.json", ROOT / "runs"))

    def test_dnsbench_home_moves_both(self):
        home = self.elsewhere / "data"
        self.assertEqual(
            paths.resolve(environ={"DNSBENCH_HOME": str(home)}),
            paths.DataPaths(home / "config.json", home / "runs"),
        )
        # ~ is expanded and a relative path is taken from the current directory; blank means unset
        self.assertEqual(paths.data_home({"DNSBENCH_HOME": "~/x"}), Path.home() / "x")
        self.assertEqual(paths.data_home({"DNSBENCH_HOME": "rel"}), Path(os.getcwd()) / "rel")
        self.assertEqual(paths.data_home({"DNSBENCH_HOME": "  "}), ROOT)

    def test_flags_override_each_path(self):
        home = {"DNSBENCH_HOME": str(self.elsewhere)}
        got = paths.resolve(config="c.json", environ=home)
        self.assertEqual(got, paths.DataPaths(Path(os.getcwd()) / "c.json", self.elsewhere / "runs"))
        got = paths.resolve(runs_dir=self.elsewhere / "r", environ=home)
        self.assertEqual(got, paths.DataPaths(self.elsewhere / "config.json", self.elsewhere / "r"))

    def test_outside_a_checkout_it_asks_for_dnsbench_home(self):
        with self.assertRaises(paths.DataHomeError) as cm:
            paths.resolve(environ={}, checkout=self.elsewhere)
        self.assertIn("DNSBENCH_HOME", str(cm.exception))
        with self.assertRaises(paths.DataHomeError):
            paths.resolve(config="c.json", environ={}, checkout=self.elsewhere)  # runs/ has nowhere to go
        # Both flags need no data home at all.
        got = paths.resolve(config="c.json", runs_dir="r", environ={}, checkout=self.elsewhere)
        self.assertEqual(got, paths.DataPaths(Path(os.getcwd()) / "c.json", Path(os.getcwd()) / "r"))


if __name__ == "__main__":
    unittest.main()
