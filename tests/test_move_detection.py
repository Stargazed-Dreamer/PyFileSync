"""文件移动识别功能单元测试

覆盖：
- PreviewManager 移动识别（元信息/哈希/增量开关/过滤）
- BackupWorker 执行 move 操作
- adb_bridge.move_file / hash_file
- config 与 progress 的 move 字段读写往返
- adb push/pull -p 参数在旧版 adb 上的自动降级

所有测试使用临时目录与 mock，不依赖真实设备与真实配置。
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import adb_bridge
from src import progress as progress_mod
from src.config import load_config, write_config
from src.models import FileOperation, PathRule
from src.preview import PreviewManager
from src.worker import BackupWorker


class TestPreviewMoveDetectionMeta(unittest.TestCase):
    """元信息识别：文件名 + 大小 + 修改时间"""

    def _setup_dirs(self, root: Path, payload: str = 'payload-data') -> tuple[Path, Path]:
        """源侧新位置 b/file.txt，目标侧旧位置 a/file.txt（同大小与修改时间）"""
        src = root / 'src'
        des = root / 'des'
        (src / 'b').mkdir(parents=True)
        (des / 'a').mkdir(parents=True)
        src_file = src / 'b' / 'file.txt'
        src_file.write_text(payload, encoding='utf-8')
        shutil.copy2(str(src_file), str(des / 'a' / 'file.txt'))  # 保留 mtime
        return src, des

    def test_sync_meta_mode_converts_copy_delete_into_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup_dirs(Path(tmp))
            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='meta')

            pm = PreviewManager()
            ops = pm.preview_operations([rule])

            moves = [op for op in ops if op.operation == 'move']
            copies = [op for op in ops if op.operation == 'copy']
            deletes = [op for op in ops if op.operation == 'delete']
            self.assertEqual(len(moves), 1)
            self.assertEqual(copies, [])
            self.assertEqual(deletes, [])
            self.assertEqual(moves[0].src_path, str(des / 'a' / 'file.txt'))
            self.assertEqual(moves[0].des_path, str(des / 'b' / 'file.txt'))
            self.assertEqual(moves[0].operation_location, 'target')

    def test_sync_meta_mode_tolerance_within_10_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup_dirs(Path(tmp))
            des_file = des / 'a' / 'file.txt'
            src_mtime = os.path.getmtime(str(src / 'b' / 'file.txt'))
            os.utime(str(des_file), (src_mtime + 5, src_mtime + 5))  # 差 5 秒应匹配

            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='meta')
            ops = PreviewManager().preview_operations([rule])

            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 1)

    def test_sync_meta_mode_rejects_large_mtime_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup_dirs(Path(tmp))
            des_file = des / 'a' / 'file.txt'
            src_mtime = os.path.getmtime(str(src / 'b' / 'file.txt'))
            os.utime(str(des_file), (src_mtime + 3600, src_mtime + 3600))

            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='meta')
            ops = PreviewManager().preview_operations([rule])

            # 不匹配：退回普通 复制 + 删除
            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 0)
            self.assertEqual(sum(1 for op in ops if op.operation == 'copy'), 1)
            self.assertEqual(sum(1 for op in ops if op.operation == 'delete'), 1)

    def test_meta_mode_does_not_match_rename(self):
        """改名（文件名不同）时元信息识别不匹配，需哈希识别"""
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup_dirs(Path(tmp))
            (src / 'b' / 'file.txt').rename(src / 'b' / 'renamed.txt')

            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='meta')
            ops = PreviewManager().preview_operations([rule])

            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 0)


class TestPreviewMoveDetectionHash(unittest.TestCase):
    """哈希识别：大小预筛 + MD5，可识别改名移动"""

    def test_hash_mode_matches_renamed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'src'
            des = root / 'des'
            (src / 'new_dir').mkdir(parents=True)
            (des / 'old_dir').mkdir(parents=True)

            src_file = src / 'new_dir' / 'renamed.txt'
            src_file.write_text('identical-content', encoding='utf-8')
            shutil.copy2(str(src_file), str(des / 'old_dir' / 'original.txt'))

            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='hash')
            ops = PreviewManager().preview_operations([rule])

            moves = [op for op in ops if op.operation == 'move']
            self.assertEqual(len(moves), 1)
            self.assertEqual(moves[0].src_path, str(des / 'old_dir' / 'original.txt'))
            self.assertEqual(moves[0].des_path, str(des / 'new_dir' / 'renamed.txt'))

    def test_hash_mode_rejects_same_size_different_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'src'
            des = root / 'des'
            src.mkdir()
            des.mkdir()

            (src / 'file.txt').write_text('AAAA', encoding='utf-8')
            (des / 'file.txt.old').write_text('BBBB', encoding='utf-8')  # 同大小不同内容

            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='hash')
            ops = PreviewManager().preview_operations([rule])

            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 0)
            self.assertEqual(sum(1 for op in ops if op.operation == 'copy'), 1)
            self.assertEqual(sum(1 for op in ops if op.operation == 'delete'), 1)


class TestPreviewMoveDetectionModes(unittest.TestCase):
    """启用条件：完全同步直接生效；增量更新需勾选开关"""

    def _setup(self, root: Path) -> tuple[Path, Path]:
        src = root / 'src'
        des = root / 'des'
        (src / 'b').mkdir(parents=True)
        (des / 'a').mkdir(parents=True)
        src_file = src / 'b' / 'file.txt'
        src_file.write_text('payload', encoding='utf-8')
        shutil.copy2(str(src_file), str(des / 'a' / 'file.txt'))
        return src, des

    def test_incremental_without_checkbox_keeps_legacy_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup(Path(tmp))
            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='overwrite', change_mode='incremental',
                            move_mode='meta', move_in_incremental=False)

            ops = PreviewManager().preview_operations([rule])

            # 增量更新未启用移动识别：只复制新文件，不删除也不移动
            self.assertEqual(sum(1 for op in ops if op.operation == 'copy'), 1)
            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 0)
            self.assertEqual(sum(1 for op in ops if op.operation == 'delete'), 0)

    def test_incremental_with_checkbox_generates_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup(Path(tmp))
            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='overwrite', change_mode='incremental',
                            move_mode='meta', move_in_incremental=True)

            ops = PreviewManager().preview_operations([rule])

            moves = [op for op in ops if op.operation == 'move']
            self.assertEqual(len(moves), 1)
            self.assertEqual(sum(1 for op in ops if op.operation == 'copy'), 0)
            self.assertEqual(moves[0].des_path, str(des / 'b' / 'file.txt'))

    def test_move_none_mode_never_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup(Path(tmp))
            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='none')

            ops = PreviewManager().preview_operations([rule])

            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 0)
            self.assertEqual(sum(1 for op in ops if op.operation == 'copy'), 1)
            self.assertEqual(sum(1 for op in ops if op.operation == 'delete'), 1)

    def test_filtered_gone_file_not_moved(self):
        """被过滤的旧位置文件不参与移动识别"""
        with tempfile.TemporaryDirectory() as tmp:
            src, des = self._setup(Path(tmp))
            rule = PathRule(src_dir=str(src), des_dir=str(des),
                            duplicate_mode='check', change_mode='sync',
                            move_mode='meta', excludes=['a'])

            ops = PreviewManager().preview_operations([rule])

            self.assertEqual(sum(1 for op in ops if op.operation == 'move'), 0)
            self.assertEqual(sum(1 for op in ops if op.operation == 'delete'), 0)
            self.assertEqual(sum(1 for op in ops if op.operation == 'copy'), 1)


class TestWorkerMoveExecution(unittest.TestCase):
    """BackupWorker 执行 move 操作"""

    def test_move_operation_relocates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target'
            target.mkdir()
            old = target / 'old.txt'
            old.write_text('payload', encoding='utf-8')

            op = FileOperation('move', str(old), str(target / 'sub' / 'new.txt'),
                               old.stat().st_size, operation_location='target')
            worker = BackupWorker([op], 'backup', False, datetime.now(),
                                  'per_rule', copy_backend='python')
            finished = []
            worker.finished.connect(finished.append)
            worker.run()

            self.assertEqual(op.status, 'success')
            self.assertEqual(finished, [True])
            self.assertFalse(old.exists())
            self.assertEqual((target / 'sub' / 'new.txt').read_text(encoding='utf-8'),
                             'payload')

    def test_move_failure_marked_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            op = FileOperation('move', str(Path(tmp) / 'missing.txt'),
                               str(Path(tmp) / 'new.txt'), 1,
                               operation_location='target')
            worker = BackupWorker([op], 'backup', False, datetime.now(),
                                  'per_rule', copy_backend='python')
            worker.run()
            self.assertEqual(op.status, 'failed')


class TestAdbBridgeMoveAndHash(unittest.TestCase):
    """adb_bridge.move_file / hash_file"""

    def test_move_file_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'a.txt'
            dst = Path(tmp) / 'b.txt'
            src.write_text('data', encoding='utf-8')

            adb_bridge.move_file(str(src), str(dst))

            self.assertFalse(src.exists())
            self.assertEqual(dst.read_text(encoding='utf-8'), 'data')

    def test_move_file_cross_side_raises(self):
        with self.assertRaises(ValueError):
            adb_bridge.move_file('/path/to/local.txt', 'adb://SERIAL123456789/sdcard/x.txt')

    def test_move_file_cross_device_raises(self):
        with self.assertRaises(ValueError):
            adb_bridge.move_file('adb://SERIAL_A/sdcard/x.txt',
                                 'adb://SERIAL_B/sdcard/y.txt')

    def test_move_file_adb_same_device_uses_shell_mv(self):
        with patch('src.adb_bridge._adb_shell') as shell_mock:
            adb_bridge.move_file('adb://SERIAL123456789/sdcard/old.txt',
                                 'adb://SERIAL123456789/sdcard/dir/new.txt')
        shell_mock.assert_called_once()
        cmd = shell_mock.call_args.args[0]
        self.assertIn('mv', cmd)
        self.assertIn('/sdcard/old.txt', cmd)
        self.assertIn('/sdcard/dir/new.txt', cmd)

    def test_hash_file_local_md5(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'x.txt'
            f.write_text('abc', encoding='utf-8')
            # 'abc' 的 MD5 为固定已知值
            self.assertEqual(adb_bridge.hash_file(str(f)),
                             '900150983cd24fb0d6963f7d28e17f72')

    def test_hash_file_adb_parses_md5sum_output(self):
        with patch('src.adb_bridge._adb_shell',
                   return_value='D41D8CD98F00B204E9800998ECF8427E /sdcard/x.txt\n'):
            result = adb_bridge.hash_file('adb://SERIAL123456789/sdcard/x.txt')
        self.assertEqual(result, 'd41d8cd98f00b204e9800998ecf8427e')

    def test_hash_file_adb_bad_output_raises(self):
        with patch('src.adb_bridge._adb_shell', return_value='not-a-hash\n'):
            with self.assertRaises(OSError):
                adb_bridge.hash_file('adb://SERIAL123456789/sdcard/x.txt')


class TestPreserveFlagFallback(unittest.TestCase):
    """旧版 adb 不支持 -p 时自动降级"""

    def setUp(self):
        self._orig_pull = adb_bridge._pull_preserve_supported
        self._orig_push = adb_bridge._push_preserve_supported

    def tearDown(self):
        adb_bridge._pull_preserve_supported = self._orig_pull
        adb_bridge._push_preserve_supported = self._orig_push

    def test_pull_falls_back_when_p_unsupported(self):
        results = [
            SimpleNamespace(returncode=1, stdout='', stderr='adb: unknown option: -p'),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = str(Path(tmp) / 'out.txt')
            with patch('src.adb_bridge.subprocess.run', side_effect=results) as run_mock:
                adb_bridge._adb_pull_direct('/sdcard/x.txt', local, 'SERIAL123456789', 60)

        self.assertFalse(adb_bridge._pull_preserve_supported)
        self.assertEqual(run_mock.call_count, 2)
        self.assertIn('-p', run_mock.call_args_list[0].args[0])
        self.assertNotIn('-p', run_mock.call_args_list[1].args[0])

    def test_push_falls_back_when_p_unsupported(self):
        results = [
            SimpleNamespace(returncode=1, stdout='', stderr='push: unknown option -p'),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = str(Path(tmp) / 'in.txt')
            Path(local).write_text('x', encoding='utf-8')
            with patch('src.adb_bridge.subprocess.run', side_effect=results) as run_mock:
                adb_bridge._adb_push_direct(local, '/sdcard/x.txt', 'SERIAL123456789', 60)

        self.assertFalse(adb_bridge._push_preserve_supported)
        self.assertEqual(run_mock.call_count, 2)
        self.assertIn('-p', run_mock.call_args_list[0].args[0])
        self.assertNotIn('-p', run_mock.call_args_list[1].args[0])


class TestConfigMoveFields(unittest.TestCase):
    """配置文件的移动识别字段读写"""

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = str(Path(tmp) / 'backup.txt')
            rules = [
                PathRule(src_dir='/path/to/source', des_dir='/path/to/target',
                         change_mode='sync', move_mode='meta'),
                PathRule(src_dir='/path/to/src2', des_dir='/path/to/des2',
                         change_mode='incremental', move_mode='hash',
                         move_in_incremental=True),
                PathRule(src_dir='/path/to/src3', des_dir='/path/to/des3'),
            ]
            write_config(cfg, datetime.now(), rules)
            _, loaded = load_config(cfg)

            self.assertEqual(loaded[0].move_mode, 'meta')
            self.assertFalse(loaded[0].move_in_incremental)
            self.assertEqual(loaded[1].move_mode, 'hash')
            self.assertTrue(loaded[1].move_in_incremental)
            self.assertEqual(loaded[2].move_mode, 'none')
            self.assertFalse(loaded[2].move_in_incremental)

    def test_old_config_without_move_tags_defaults_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = str(Path(tmp) / 'old.txt')
            with open(cfg, 'w', encoding='utf-8') as f:
                f.write('*2026.01.01 00:00:00\n')
                f.write('/path/to/source → /path/to/target |相同文件:覆盖 |增删文件处理:完全同步 |状态:启用\n')
            _, loaded = load_config(cfg)
            self.assertEqual(loaded[0].move_mode, 'none')
            self.assertFalse(loaded[0].move_in_incremental)


class TestProgressMovePersistence(unittest.TestCase):
    """断点续传文件的 move 操作与新字段持久化"""

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress_file = str(Path(tmp) / 'progress.txt')
            rules = [PathRule(src_dir='/path/to/source', des_dir='/path/to/target',
                              change_mode='sync', move_mode='hash',
                              move_in_incremental=True)]
            ops = [
                FileOperation('copy', '/path/to/source/a.txt',
                              '/path/to/target/a.txt', 1, rule_index=0),
                FileOperation('move', '/path/to/target/old.txt',
                              '/path/to/target/new.txt', 2,
                              operation_location='target', rule_index=0),
                FileOperation('delete', '', '/path/to/target/gone.txt', 3,
                              operation_location='target', rule_index=0),
            ]
            with patch.object(progress_mod, 'PROGRESS_FILE', progress_file):
                progress_mod.write_progress(ops, 0, 'backup', 'per_rule',
                                            False, datetime.now(), rules)
                (loaded_ops, idx, mode, skip_older, ts, loaded_rules,
                 info, dup_mode) = progress_mod.read_progress()

            self.assertEqual(len(loaded_ops), 3)
            move_op = loaded_ops[1]
            self.assertEqual(move_op.operation, 'move')
            self.assertEqual(move_op.src_path, '/path/to/target/old.txt')
            self.assertEqual(move_op.des_path, '/path/to/target/new.txt')
            self.assertEqual(move_op.rule_index, 0)
            self.assertEqual(move_op.operation_location, 'target')
            self.assertEqual(loaded_rules[0].move_mode, 'hash')
            self.assertTrue(loaded_rules[0].move_in_incremental)


if __name__ == '__main__':
    unittest.main()
