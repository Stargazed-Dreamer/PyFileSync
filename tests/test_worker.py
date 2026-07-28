from types import SimpleNamespace
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from models import FileOperation
from worker import BackupWorker


class BackupWorkerTests(unittest.TestCase):
    def test_parallel_copy_batch_reports_completed_operations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            source.mkdir()

            operations = []
            for index in range(4):
                src = source / f"file-{index}.txt"
                src.write_text(f"payload-{index}", encoding="utf-8")
                operations.append(
                    FileOperation(
                        "copy",
                        str(src),
                        str(target / src.name),
                        src.stat().st_size,
                        rule_index=0,
                    )
                )

            worker = BackupWorker(
                operations,
                "backup",
                False,
                datetime.now(),
                "overwrite",
                copy_workers=2,
            )
            progress = []
            statuses = []
            finished = []
            worker.progress_updated.connect(lambda current, total: progress.append((current, total)))
            worker.operation_updated.connect(lambda path, status: statuses.append(status))
            worker.finished.connect(finished.append)

            worker.run()

            self.assertEqual(worker.copy_workers, 2)
            self.assertEqual(finished, [True])
            self.assertEqual(statuses, ["success"] * 4)
            self.assertEqual(progress[-1], (4, 4))
            self.assertTrue(all((target / f"file-{index}.txt").exists() for index in range(4)))

    def test_copy_then_delete_keeps_delete_after_copy_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            stale = target / "stale.txt"
            stale.write_text("stale", encoding="utf-8")

            source_file = source / "current.txt"
            source_file.write_text("current", encoding="utf-8")
            operations = [
                FileOperation("copy", str(source_file), str(target / source_file.name), source_file.stat().st_size, rule_index=0),
                FileOperation("delete", "", str(stale), stale.stat().st_size, rule_index=0),
            ]

            worker = BackupWorker(
                operations,
                "backup",
                False,
                datetime.now(),
                "overwrite",
                copy_workers=2,
            )
            worker.run()

            self.assertEqual([op.status for op in operations], ["success", "success"])
            self.assertFalse(stale.exists())
            self.assertTrue((target / source_file.name).exists())

    def test_robocopy_batch_uses_explicit_files_and_marks_results(self):
        operations = [
            FileOperation(
                "copy",
                f"C:/source/dir/file-{index}.bin",
                f"D:/target/dir/file-{index}.bin",
                1,
                rule_index=0,
            )
            for index in range(8)
        ]
        worker = BackupWorker(
            operations,
            "backup",
            False,
            datetime.now(),
            "overwrite",
            copy_workers=4,
            copy_backend="python",
        )
        worker.copy_backend = "robocopy"

        with patch("worker.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run_mock:
            with patch.object(BackupWorker, "_destination_matches", return_value=True):
                worker.run()

        self.assertEqual(run_mock.call_count, 1)
        command = run_mock.call_args.args[0]
        self.assertIn("file-0.bin", command)
        self.assertIn("file-7.bin", command)
        self.assertIn("/MT:4", command)
        self.assertEqual([op.status for op in operations], ["success"] * 8)

    def test_failed_copy_reports_unsuccessful_completion(self):
        operation = FileOperation(
            "copy",
            "C:/source/does-not-exist.bin",
            "D:/target/does-not-exist.bin",
            1,
        )
        worker = BackupWorker(
            [operation],
            "backup",
            False,
            datetime.now(),
            "overwrite",
            copy_backend="python",
        )
        finished = []
        worker.finished.connect(finished.append)

        worker.run()

        self.assertEqual(operation.status, "failed")
        self.assertEqual(finished, [False])


if __name__ == "__main__":
    unittest.main()
