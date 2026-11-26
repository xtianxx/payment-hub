"""metrics_store.py 的归档与回看复制测试。"""

import contextlib
import errno
import io
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import metrics_store

HASH_PATTERN = re.compile(r"sha256:([0-9a-f]{64})")


def write_file(path, content, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mode is not None:
        path.chmod(mode)
    return path


def make_tsdb(root):
    root = Path(root)
    root.mkdir(parents=True)
    write_file(root / "wal" / "00000000", b"wal-one")
    write_file(root / "wal" / "00000001", b"wal-two")
    write_file(root / "chunks_head" / "000001", b"chunk-one")
    write_file(root / "meta.json", b'{"version": 1}\n')
    write_file(root / "lock", b"")
    return root


def file_snapshot(root):
    root = Path(root)
    result = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            result[relative] = path.read_bytes()
    return result


class MetricsStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def run_main(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = metrics_store.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def archive(self, source, run_dir=None):
        return self.run_main(
            "archive",
            "--source",
            str(source),
            "--run-dir",
            str(run_dir or self.run_dir),
        )

    def view(self, run_dir=None, view_dir=None):
        return self.run_main(
            "view",
            "--run-dir",
            str(run_dir or self.run_dir),
            "--view-dir",
            str(view_dir),
        )

    def test_archive_success_renames_tmp_and_preserves_metadata(self):
        source = make_tsdb(self.root / "prometheus")
        wal_file = source / "wal" / "00000000"
        wal_file.chmod(0o640)
        fixed_time = 1_700_000_000
        os.utime(str(wal_file), (fixed_time, fixed_time))
        source_wal_mode = stat.S_IMODE((source / "wal").stat().st_mode)
        source_files = file_snapshot(source)
        expected_hash, expected_count = metrics_store.tree_stat(str(source))

        code, stdout, stderr = self.archive(source)

        self.assertEqual(code, 0, stderr)
        target = self.run_dir / "metrics"
        self.assertTrue(target.is_dir())
        self.assertFalse((self.run_dir / "metrics.tmp").exists())
        self.assertTrue((target / "wal").is_dir())
        self.assertEqual(file_snapshot(target), source_files)
        self.assertEqual(file_snapshot(source), source_files)
        self.assertEqual(metrics_store.tree_stat(str(target)), (expected_hash, expected_count))

        target_wal_file = target / "wal" / "00000000"
        self.assertEqual(stat.S_IMODE(target_wal_file.stat().st_mode), 0o640)
        self.assertEqual(int(target_wal_file.stat().st_mtime), fixed_time)
        self.assertEqual(
            stat.S_IMODE((target / "wal").stat().st_mode),
            source_wal_mode,
        )

        hashes = HASH_PATTERN.findall(stdout)
        self.assertGreaterEqual(len(hashes), 2, stdout)
        self.assertTrue(all(value == expected_hash for value in hashes), stdout)
        self.assertIn("归档文件数: %d" % expected_count, stdout)

    def test_archive_refuses_existing_metrics_or_metrics_tmp(self):
        for existing_name in ("metrics", "metrics.tmp"):
            with self.subTest(existing=existing_name):
                case = self.root / existing_name.replace(".", "-")
                case.mkdir()
                run_dir = case / "run"
                run_dir.mkdir()
                source = make_tsdb(case / "prometheus")
                existing = run_dir / existing_name
                existing.mkdir()
                write_file(existing / "sentinel", b"keep-me")
                existing_files = file_snapshot(existing)

                code, _, stderr = self.archive(source, run_dir=run_dir)

                self.assertEqual(code, 2, stderr)
                self.assertEqual(file_snapshot(existing), existing_files)
                other = "metrics.tmp" if existing_name == "metrics" else "metrics"
                self.assertFalse((run_dir / other).exists())
                self.assertIn("拒绝覆盖", stderr)

    def test_archive_rejects_source_without_wal(self):
        source = self.root / "prometheus"
        write_file(source / "meta.json", b"{}")

        code, _, stderr = self.archive(source)

        self.assertEqual(code, 2, stderr)
        self.assertIn("wal/", stderr)
        self.assertFalse((self.run_dir / "metrics").exists())
        self.assertFalse((self.run_dir / "metrics.tmp").exists())

    def test_archive_rejects_missing_source(self):
        code, _, stderr = self.archive(self.root / "missing")

        self.assertEqual(code, 2, stderr)
        self.assertIn("不存在", stderr)
        self.assertFalse((self.run_dir / "metrics.tmp").exists())

    def test_archive_verification_failure_keeps_metrics_tmp(self):
        source = make_tsdb(self.root / "prometheus")
        real_copy = metrics_store.copy_tree

        def corrupt_copy(source_path, destination_path):
            real_copy(source_path, destination_path)
            write_file(Path(destination_path) / "wal" / "99999999", b"unexpected")

        with mock.patch.object(metrics_store, "copy_tree", side_effect=corrupt_copy):
            code, _, stderr = self.archive(source)

        self.assertEqual(code, 3, stderr)
        temporary = self.run_dir / "metrics.tmp"
        self.assertTrue(temporary.is_dir())
        self.assertFalse((self.run_dir / "metrics").exists())
        self.assertTrue((temporary / "wal" / "99999999").is_file())
        self.assertIn("校验失败", stderr)

    def test_archive_rename_failure_keeps_metrics_tmp(self):
        source = make_tsdb(self.root / "prometheus")
        expected_hash, expected_count = metrics_store.tree_stat(str(source))

        with mock.patch.object(
            metrics_store.os,
            "replace",
            side_effect=OSError(errno.EXDEV, "Invalid cross-device link"),
        ):
            code, _, stderr = self.archive(source)

        self.assertEqual(code, 2, stderr)
        temporary = self.run_dir / "metrics.tmp"
        self.assertTrue(temporary.is_dir())
        self.assertFalse((self.run_dir / "metrics").exists())
        self.assertEqual(
            metrics_store.tree_stat(str(temporary)),
            (expected_hash, expected_count),
        )
        self.assertIn("保留", stderr)
        self.assertIn("metrics.tmp", stderr)

    def test_archive_rejects_symlink_in_source(self):
        source = make_tsdb(self.root / "prometheus")
        try:
            os.symlink(str(source / "meta.json"), str(source / "wal" / "link"))
        except (OSError, NotImplementedError) as error:
            self.skipTest("当前文件系统不支持符号链接: %s" % error)

        code, _, stderr = self.archive(source)

        self.assertEqual(code, 2, stderr)
        self.assertIn("符号链接", stderr)
        self.assertFalse((self.run_dir / "metrics").exists())
        self.assertFalse((self.run_dir / "metrics.tmp").exists())

    def test_view_success_copies_contents_and_keeps_archive_unchanged(self):
        source = make_tsdb(self.root / "prometheus")
        archive_dir = self.run_dir / "metrics"
        shutil.copytree(str(source), str(archive_dir))
        archive_files = file_snapshot(archive_dir)
        archive_hash, archive_count = metrics_store.tree_stat(str(archive_dir))
        view_dir = self.root / "view"

        code, stdout, stderr = self.view(view_dir=view_dir)

        self.assertEqual(code, 0, stderr)
        self.assertTrue((view_dir / "wal").is_dir())
        self.assertFalse((view_dir / "metrics").exists())
        self.assertEqual(file_snapshot(view_dir), archive_files)
        self.assertEqual(
            metrics_store.tree_stat(str(view_dir)),
            (archive_hash, archive_count),
        )
        self.assertEqual(file_snapshot(archive_dir), archive_files)
        self.assertEqual(
            metrics_store.tree_stat(str(archive_dir)),
            (archive_hash, archive_count),
        )
        hashes = HASH_PATTERN.findall(stdout)
        self.assertGreaterEqual(len(hashes), 2, stdout)
        self.assertTrue(all(value == archive_hash for value in hashes), stdout)
        self.assertIn(str(view_dir), stdout)

    def test_view_accepts_existing_empty_directory(self):
        source = make_tsdb(self.root / "prometheus")
        shutil.copytree(str(source), str(self.run_dir / "metrics"))
        view_dir = self.root / "view"
        view_dir.mkdir()

        code, _, stderr = self.view(view_dir=view_dir)

        self.assertEqual(code, 0, stderr)
        self.assertTrue((view_dir / "wal").is_dir())
        self.assertFalse((view_dir / "metrics").exists())

    def test_view_refuses_non_empty_directory(self):
        source = make_tsdb(self.root / "prometheus")
        archive_dir = self.run_dir / "metrics"
        shutil.copytree(str(source), str(archive_dir))
        archive_files = file_snapshot(archive_dir)
        archive_hash = metrics_store.tree_stat(str(archive_dir))
        view_dir = self.root / "view"
        view_dir.mkdir()
        write_file(view_dir / "sentinel", b"keep-me")

        code, _, stderr = self.view(view_dir=view_dir)

        self.assertEqual(code, 2, stderr)
        self.assertIn("非空", stderr)
        self.assertEqual((view_dir / "sentinel").read_bytes(), b"keep-me")
        self.assertEqual(file_snapshot(archive_dir), archive_files)
        self.assertEqual(metrics_store.tree_stat(str(archive_dir)), archive_hash)

    def test_view_requires_archive_with_wal(self):
        view_dir = self.root / "view-missing-archive"
        code, _, stderr = self.view(view_dir=view_dir)
        self.assertEqual(code, 2, stderr)
        self.assertIn("不存在", stderr)

        metrics_dir = self.run_dir / "metrics"
        write_file(metrics_dir / "meta.json", b"{}")
        code, _, stderr = self.view(view_dir=self.root / "view-missing-wal")
        self.assertEqual(code, 2, stderr)
        self.assertIn("wal/", stderr)

    def test_view_refuses_view_dir_inside_archive(self):
        source = make_tsdb(self.root / "prometheus")
        archive_dir = self.run_dir / "metrics"
        shutil.copytree(str(source), str(archive_dir))
        archive_hash = metrics_store.tree_stat(str(archive_dir))

        for view_dir in (archive_dir, archive_dir / "copy"):
            with self.subTest(view_dir=str(view_dir)):
                code, _, stderr = self.view(view_dir=view_dir)
                self.assertEqual(code, 2, stderr)
                self.assertIn("位于归档目录内", stderr)
                self.assertEqual(
                    metrics_store.tree_stat(str(archive_dir)),
                    archive_hash,
                )
        self.assertFalse((archive_dir / "copy").exists())

    def test_view_verification_failure_returns_three(self):
        source = make_tsdb(self.root / "prometheus")
        archive_dir = self.run_dir / "metrics"
        shutil.copytree(str(source), str(archive_dir))
        archive_hash = metrics_store.tree_stat(str(archive_dir))
        real_copy = metrics_store.copy_tree

        def corrupt_copy(source_path, destination_path):
            real_copy(source_path, destination_path)
            write_file(Path(destination_path) / "wal" / "00000000", b"corrupted")

        view_dir = self.root / "view"
        with mock.patch.object(metrics_store, "copy_tree", side_effect=corrupt_copy):
            code, _, stderr = self.view(view_dir=view_dir)

        self.assertEqual(code, 3, stderr)
        self.assertIn("回看副本校验失败", stderr)
        self.assertTrue((view_dir / "wal").is_dir())
        self.assertEqual(metrics_store.tree_stat(str(archive_dir)), archive_hash)

    def test_view_detects_archive_change_during_copy(self):
        source = make_tsdb(self.root / "prometheus")
        archive_dir = self.run_dir / "metrics"
        shutil.copytree(str(source), str(archive_dir))
        archive_hash = metrics_store.tree_stat(str(archive_dir))
        real_copy = metrics_store.copy_tree

        def mutate_archive(source_path, destination_path):
            real_copy(source_path, destination_path)
            write_file(Path(source_path) / "wal" / "99999999", b"late-write")

        view_dir = self.root / "view"
        with mock.patch.object(metrics_store, "copy_tree", side_effect=mutate_archive):
            code, _, stderr = self.view(view_dir=view_dir)

        self.assertEqual(code, 3, stderr)
        self.assertIn("归档目录在复制期间发生变化", stderr)
        self.assertNotEqual(metrics_store.tree_stat(str(archive_dir)), archive_hash)


if __name__ == "__main__":
    unittest.main()
