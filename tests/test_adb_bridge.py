"""adb_bridge.py 单元测试

测试路径解析、设备管理、文件扫描、复制方向判断。
ADB 命令执行通过 mock subprocess 验证调用逻辑，不依赖真实设备。
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, call

from src import adb_bridge


class TestPathUtils(unittest.TestCase):
    """测试路径工具函数"""

    def test_is_adb_path_positive(self):
        self.assertTrue(adb_bridge.is_adb_path('adb://serial/sdcard'))
        self.assertTrue(adb_bridge.is_adb_path('adb://ABC123/sdcard/DCIM/Camera'))

    def test_is_adb_path_negative(self):
        self.assertFalse(adb_bridge.is_adb_path(r'C:\Users\test'))
        self.assertFalse(adb_bridge.is_adb_path('/home/user'))
        self.assertFalse(adb_bridge.is_adb_path(''))
        self.assertFalse(adb_bridge.is_adb_path('http://example.com'))

    def test_parse_adb_path_full(self):
        serial, path = adb_bridge.parse_adb_path('adb://SERIAL123456789/sdcard/DCIM')
        self.assertEqual(serial, 'SERIAL123456789')
        self.assertEqual(path, '/sdcard/DCIM')

    def test_parse_adb_path_root(self):
        serial, path = adb_bridge.parse_adb_path('adb://serial123/')
        self.assertEqual(serial, 'serial123')
        self.assertEqual(path, '/')

    def test_parse_adb_path_no_path(self):
        """仅有序列号无路径时返回根路径"""
        serial, path = adb_bridge.parse_adb_path('adb://serial_only')
        self.assertEqual(serial, 'serial_only')
        self.assertEqual(path, '/')

    def test_parse_adb_path_raises_on_local(self):
        with self.assertRaises(ValueError):
            adb_bridge.parse_adb_path(r'C:\local\path')

    def test_build_adb_path(self):
        result = adb_bridge.build_adb_path('serial123', '/sdcard/DCIM')
        self.assertEqual(result, 'adb://serial123/sdcard/DCIM')

    def test_build_adb_path_adds_leading_slash(self):
        result = adb_bridge.build_adb_path('serial', 'sdcard/DCIM')
        self.assertEqual(result, 'adb://serial/sdcard/DCIM')

    def test_build_and_parse_roundtrip(self):
        """build → parse 往返一致"""
        original = 'adb://DEVICE42/sdcard/Music/song.mp3'
        serial, path = adb_bridge.parse_adb_path(original)
        rebuilt = adb_bridge.build_adb_path(serial, path)
        self.assertEqual(rebuilt, original)


class TestListDevices(unittest.TestCase):
    """测试设备管理（mock subprocess）"""

    def _mock_run(self, stdout: str):
        """创建 mock subprocess.run 的返回值"""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    def test_list_devices_parses_connected_device(self):
        stdout = (
            'List of devices attached\n'
            'SERIAL123456789    device product:foo model:Pixel_7 device:bar transport_id:1\n'
        )
        with patch('src.adb_bridge.subprocess.run', return_value=self._mock_run(stdout)):
            devices = adb_bridge.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['serial'], 'SERIAL123456789')
        self.assertEqual(devices[0]['model'], 'Pixel_7')
        self.assertEqual(devices[0]['state'], 'device')

    def test_list_devices_skips_unauthorized(self):
        stdout = (
            'List of devices attached\n'
            'device1    device model:Phone_A\n'
            'device2    unauthorized\n'
            'device3    offline\n'
        )
        with patch('src.adb_bridge.subprocess.run', return_value=self._mock_run(stdout)):
            devices = adb_bridge.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['serial'], 'device1')

    def test_list_devices_empty(self):
        stdout = 'List of devices attached\n'
        with patch('src.adb_bridge.subprocess.run', return_value=self._mock_run(stdout)):
            devices = adb_bridge.list_devices()
        self.assertEqual(devices, [])

    def test_is_device_connected(self):
        stdout = 'List of devices attached\nSERIAL_X    device model:TestPhone\n'
        with patch('src.adb_bridge.subprocess.run', return_value=self._mock_run(stdout)):
            self.assertTrue(adb_bridge.is_device_connected('SERIAL_X'))
            self.assertFalse(adb_bridge.is_device_connected('NOT_CONNECTED'))


class TestScanFilesLocal(unittest.TestCase):
    """测试本地目录扫描"""

    def test_scan_local_returns_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.txt').write_text('hello', encoding='utf-8')
            (root / 'sub').mkdir()
            (root / 'sub' / 'b.txt').write_text('world!!', encoding='utf-8')

            results = adb_bridge.scan_files(str(root))

            self.assertEqual(len(results), 2)
            paths = {os.path.basename(r[0]) for r in results}
            self.assertEqual(paths, {'a.txt', 'b.txt'})

            # 验证元数据
            for filepath, size, mtime in results:
                self.assertTrue(os.path.exists(filepath))
                self.assertEqual(size, os.path.getsize(filepath))
                self.assertAlmostEqual(mtime, os.path.getmtime(filepath), places=1)

    def test_scan_local_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = adb_bridge.scan_files(tmp)
            self.assertEqual(results, [])


class TestCopyFileDirection(unittest.TestCase):
    """测试 copy_file 方向判断（mock subprocess 验证调用 pull/push/copy2）"""

    def test_copy_adb_to_local_calls_pull(self):
        """src=adb://, dst=local → adb pull"""
        with patch('src.adb_bridge._adb_pull') as mock_pull:
            adb_bridge.copy_file(
                'adb://SERIAL/sdcard/file.txt',
                r'C:\local\file.txt'
            )
            mock_pull.assert_called_once_with(
                '/sdcard/file.txt', r'C:\local\file.txt', 'SERIAL'
            )

    def test_copy_local_to_adb_calls_push(self):
        """src=local, dst=adb:// → adb push"""
        with patch('src.adb_bridge._adb_push') as mock_push:
            adb_bridge.copy_file(
                r'C:\local\file.txt',
                'adb://SERIAL/sdcard/file.txt'
            )
            mock_push.assert_called_once_with(
                r'C:\local\file.txt', '/sdcard/file.txt', 'SERIAL'
            )

    def test_copy_local_to_local_uses_shutil(self):
        """both local → shutil.copy2"""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'src.txt'
            dst = Path(tmp) / 'dst.txt'
            src.write_text('content', encoding='utf-8')

            adb_bridge.copy_file(str(src), str(dst))

            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_text(encoding='utf-8'), 'content')

    def test_copy_both_adb_raises(self):
        """both adb:// → ValueError"""
        with self.assertRaises(ValueError):
            adb_bridge.copy_file(
                'adb://DEV1/sdcard/file.txt',
                'adb://DEV2/sdcard/file.txt'
            )


class TestAdbShellErrorHandling(unittest.TestCase):
    """测试 ADB 命令错误处理"""

    def test_adb_shell_raises_on_nonzero_return(self):
        result = SimpleNamespace(returncode=1, stdout='', stderr='error: device not found')
        with patch('src.adb_bridge.subprocess.run', return_value=result):
            with self.assertRaises(OSError) as ctx:
                adb_bridge._adb_shell('ls', 'SERIAL')
            self.assertIn('device not found', str(ctx.exception))

    def test_adb_shell_raises_on_timeout(self):
        import subprocess
        with patch('src.adb_bridge.subprocess.run', side_effect=subprocess.TimeoutExpired('adb', 5)):
            with self.assertRaises(OSError) as ctx:
                adb_bridge._adb_shell('ls', 'SERIAL')
            self.assertIn('超时', str(ctx.exception))

    def test_adb_shell_raises_on_missing_adb(self):
        with patch('src.adb_bridge.subprocess.run', side_effect=FileNotFoundError()):
            with self.assertRaises(OSError) as ctx:
                adb_bridge._adb_shell('ls', 'SERIAL')
            self.assertIn('未找到', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
