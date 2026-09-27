from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from samples import V1_FIXTURES, V1_RUN_IDS, make_run

from dnsbench import storage


def raw_save(repo: storage.RunRepository, run: dict) -> Path:
    """Write a run file as-is, the way an older dns-bench (or a person) might have."""
    repo.dir.mkdir(parents=True, exist_ok=True)
    path = repo.dir / f"{run['id']}.json"
    path.write_text(json.dumps(run), encoding="utf-8")
    return path


class RunIdTest(unittest.TestCase):
    def test_valid_run_id(self):
        for good in ("20260925T023456Z", "20260925T023456Z-2", "20260925T023456Z-15"):
            self.assertTrue(storage.valid_run_id(good), good)
        for bad in (
            "",
            "latest",
            "../etc/passwd",
            "20260925T023456Z/../x",
            "20260925T023456",
            "20260925T023456Z-",
            "20260925T023456Z-a",
            "2026-09-25",
            None,
            5,
            "20260925T023456Z\n",
        ):
            self.assertFalse(storage.valid_run_id(bad), bad)

    def test_sort_key_puts_suffixes_after_their_base(self):
        ids = ["20260301T000000Z-2", "20260101T000000Z", "20260301T000000Z", "20260301T000000Z-10"]
        self.assertEqual(
            sorted(ids, key=storage.id_sort_key),
            ["20260101T000000Z", "20260301T000000Z", "20260301T000000Z-2", "20260301T000000Z-10"],
        )


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = storage.RunRepository(Path(self.tmp.name) / "runs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_writable(self):
        self.assertIsNone(self.repo.check_writable())
        self.assertTrue(self.repo.dir.is_dir())
        self.assertEqual(list(self.repo.dir.iterdir()), [])  # the probe file is gone
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        self.assertEqual(storage.RunRepository(blocker / "runs").check_writable(), "Not a directory")

    def test_save_writes_the_record_and_the_report_as_given(self):
        run = make_run()
        path = self.repo.save(run, "the report\n")
        self.assertEqual(path, self.repo.dir / "20260925T023456Z.json")
        self.assertEqual(json.loads(path.read_text()), run)
        self.assertIn('\n  "id": ', path.read_text())  # pretty-printed
        self.assertEqual((self.repo.dir / "20260925T023456Z.txt").read_text(), "the report\n")
        self.assertEqual([p.name for p in self.repo.dir.iterdir() if p.name.startswith(".")], [])

    def test_id_collision_never_overwrites(self):
        p1 = self.repo.save(make_run(fast_ms=5.0), "1")
        original = p1.read_text()
        run2 = make_run(fast_ms=9.0)
        p2 = self.repo.save(run2, "2")
        p3 = self.repo.save(make_run(fast_ms=11.0), "3")
        self.assertEqual(p2.name, "20260925T023456Z-2.json")
        self.assertEqual(p3.name, "20260925T023456Z-3.json")
        self.assertEqual(run2["id"], "20260925T023456Z-2")
        self.assertEqual(p1.read_text(), original)
        self.assertEqual(json.loads(p2.read_text())["id"], "20260925T023456Z-2")
        self.assertEqual((self.repo.dir / "20260925T023456Z-3.txt").read_text(), "3")

    def test_ids_newest_first(self):
        self.assertEqual(self.repo.ids(), [])  # no directory yet
        for run_id in ("20260101T000000Z", "20260301T000000Z", "20260201T000000Z", "20260301T000000Z"):
            self.repo.save(make_run(run_id), "")
        (self.repo.dir / "notes.json").write_text("{}")  # not a run id: ignored
        self.assertEqual(
            self.repo.ids(),
            ["20260301T000000Z-2", "20260301T000000Z", "20260201T000000Z", "20260101T000000Z"],
        )
        self.assertTrue(self.repo.exists("20260201T000000Z"))
        self.assertFalse(self.repo.exists("20250101T000000Z"))
        self.assertFalse(self.repo.exists("../runs/20260201T000000Z"))

    def test_load_migrates_and_never_trusts_derived_data(self):
        run = make_run()
        run["summary"] = {"stale": True}
        run["recommendation"] = {"best": "Someone else"}
        raw_save(self.repo, run)
        loaded = self.repo.load("20260925T023456Z")
        self.assertEqual((loaded["schema"], loaded["kind"]), (1, "run"))
        self.assertNotIn("summary", loaded)
        self.assertNotIn("recommendation", loaded)
        self.assertTrue(all(r["attempts"] == 1 and r["truncated"] is False for r in loaded["results"]))

    def test_load_errors(self):
        with self.assertRaises(storage.RunNotFound):
            self.repo.load("20200101T000000Z")
        for bad in ("../../etc/passwd", "latest", "20260925T023456Z.json"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.repo.load(bad)
        self.repo.dir.mkdir(parents=True)
        cases = {
            "20260101T000000Z": "{ nope",
            "20260102T000000Z": "[1, 2, 3]",
            "20260103T000000Z": json.dumps({"results": [{"bogus": 1}]}),
            "20260104T000000Z": json.dumps({"results": "x"}),
            "20260105T000000Z": json.dumps({"schema": 2, "results": []}),
            "20260106T000000Z": json.dumps({"results": [make_run()["results"][0] | {"ms": "fast"}]}),
            "20260107T000000Z": "[" * 100000 + "]" * 100000,  # absurd nesting: the same on every Python
            "20260108T000000Z": json.dumps({"results": [], "config": "x"}),
            "20260109T000000Z": json.dumps({"results": [], "config": {"settings": "x"}}),
            "20260110T000000Z": json.dumps({"results": [], "config": {"resolvers": {}}}),
        }
        for run_id, text in cases.items():
            (self.repo.dir / f"{run_id}.json").write_text(text)
            with self.subTest(text=text[:30]), self.assertRaises(storage.CorruptRun) as cm:
                self.repo.load(run_id)
            self.assertNotIn(self.tmp.name, str(cm.exception))  # no paths: the API shows these messages
        self.assertIn("unknown run file format 2", str(storage.CorruptRun("x", "unknown run file format 2")))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_file_is_corrupt(self):
        path = raw_save(self.repo, make_run())
        path.chmod(0)
        try:
            with self.assertRaises(storage.CorruptRun) as cm:
                self.repo.load("20260925T023456Z")
            self.assertIn("Permission denied", str(cm.exception))
        finally:
            path.chmod(0o644)

    def test_stamp_changes_with_the_file(self):
        path = raw_save(self.repo, make_run())
        before = self.repo.stamp("20260925T023456Z")
        self.assertIsNotNone(before)
        path.write_text(json.dumps(make_run() | {"host": "a-longer-host-name"}))
        self.assertNotEqual(self.repo.stamp("20260925T023456Z"), before)
        self.assertIsNone(self.repo.stamp("20200101T000000Z"))

    def test_rescue_run(self):
        rescue_dir = Path(self.tmp.name) / "rescue"
        rescue_dir.mkdir()
        run = make_run()
        original = tempfile.tempdir
        tempfile.tempdir = str(rescue_dir)
        try:
            path = storage.rescue_run(run)
        finally:
            tempfile.tempdir = original
        self.assertEqual(path.parent, rescue_dir)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), run)


class V1RunFilesTest(unittest.TestCase):
    """Run files written by dns-bench 1.0.0 load as current records."""

    def setUp(self):
        # A copy, so nothing a test does can change the fixtures.
        self.tmp = tempfile.TemporaryDirectory()
        shutil.copytree(V1_FIXTURES, Path(self.tmp.name) / "runs")
        self.repo = storage.RunRepository(Path(self.tmp.name) / "runs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixtures_are_v1_records(self):
        self.assertEqual(sorted(p.stem for p in V1_FIXTURES.glob("*.json")), sorted(V1_RUN_IDS))
        for run_id in V1_RUN_IDS:
            with self.subTest(run_id=run_id):
                raw = json.loads((V1_FIXTURES / f"{run_id}.json").read_text(encoding="utf-8"))
                self.assertEqual(raw["version"], "1.0.0")
                self.assertNotIn("schema", raw)  # v1 records have no schema version
                self.assertIn("summary", raw)
                self.assertNotIn("truncated", raw["results"][0])  # added after 1.0.0

    def test_migrate_reads_them_as_schema_1(self):
        self.assertEqual(self.repo.ids(), V1_RUN_IDS)
        for run_id in V1_RUN_IDS:
            with self.subTest(run_id=run_id):
                run = self.repo.load(run_id)
                raw = json.loads((V1_FIXTURES / f"{run_id}.json").read_text(encoding="utf-8"))
                self.assertEqual((run["schema"], run["kind"], run["id"]), (1, "run", run_id))
                self.assertEqual(run["host"], "example-host")
                self.assertEqual(len(run["results"]), len(raw["results"]))
                for got, was in zip(run["results"], raw["results"], strict=True):
                    self.assertEqual(got, {"attempts": 1, "truncated": False, **was})
                self.assertEqual(run["config"], raw["config"])


if __name__ == "__main__":
    unittest.main()
