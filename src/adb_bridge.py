"""ADB 文件操作抽象层

提供统一的文件操作接口，根据路径类型（本地 vs adb:// URI）自动路由到对应后端。
所有 ADB 命令通过 subprocess + UTF-8 编码执行，正确处理中文文件名。
"""

import os
import shutil
import subprocess

# ADB 路径前缀
ADB_PREFIX = 'adb://'

# 超时（秒）
SHELL_TIMEOUT = 5          # 设备预检等快速命令
TRANSFER_TIMEOUT = 3600    # 单文件传输（大文件如视频）


# ── 路径工具 ──────────────────────────────────────────

def is_adb_path(path: str) -> bool:
    """判断路径是否为 adb:// URI"""
    return path.startswith(ADB_PREFIX)


def parse_adb_path(path: str) -> tuple[str, str]:
    """解析 adb://serial/path → (serial, android_path)

    例：parse_adb_path('adb://SERIAL123456789/sdcard/DCIM')
        → ('SERIAL123456789', '/sdcard/DCIM')
    """
    if not is_adb_path(path):
        raise ValueError(f"不是 ADB 路径: {path}")
    rest = path[len(ADB_PREFIX):]
    slash_idx = rest.find('/')
    if slash_idx == -1:
        # 仅有序列号无路径
        return rest, '/'
    serial = rest[:slash_idx]
    android_path = rest[slash_idx:]
    if not android_path.startswith('/'):
        android_path = '/' + android_path
    return serial, android_path


def build_adb_path(serial: str, android_path: str) -> str:
    """组装 adb://serial/path"""
    if not android_path.startswith('/'):
        android_path = '/' + android_path
    return f"{ADB_PREFIX}{serial}{android_path}"


# ── 设备管理 ──────────────────────────────────────────

def is_adb_available() -> bool:
    """检查 adb 命令是否可用（已安装且在 PATH 中）

    用于无 ADB 环境下（如项目迁移到新机器）优雅降级，
    而不是在调用时才抛出 FileNotFoundError。
    """
    import shutil
    return shutil.which('adb') is not None


def list_devices() -> list[dict]:
    """列出已连接的 ADB 设备

    返回 [{"serial": str, "model": str, "state": str}]
    仅返回 state 为 device 的设备（跳过 unauthorized/offline）
    """
    result = subprocess.run(
        ['adb', 'devices', '-l'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=SHELL_TIMEOUT,
    )
    devices = []
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('List of devices'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        state = parts[1]
        if state != 'device':
            continue
        model = serial  # 默认用序列号
        for part in parts[2:]:
            if part.startswith('model:'):
                model = part[len('model:'):]
                break
        devices.append({'serial': serial, 'model': model, 'state': state})
    return devices


def is_device_connected(serial: str) -> bool:
    """检查指定设备是否已连接且状态为 device"""
    return any(d['serial'] == serial for d in list_devices())


# ── ADB 命令内部封装 ──────────────────────────────────

def _adb_shell(cmd: str, serial: str, timeout: int = SHELL_TIMEOUT) -> str:
    """执行 adb shell 命令，返回 stdout

    失败时抛出 OSError（与 os 模块行为一致，便于现有错误处理复用）
    errors='replace' 防止 adb 输出非 UTF-8 字节时 UnicodeDecodeError
    """
    try:
        result = subprocess.run(
            ['adb', '-s', serial, 'shell', cmd],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or f"adb shell 返回码 {result.returncode}"
            raise OSError(f"adb shell 失败: {err}")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise OSError(f"adb shell 超时 ({timeout}s): {cmd}")
    except FileNotFoundError:
        raise OSError("adb 命令未找到，请确认 Android Platform Tools 已安装并在 PATH 中")


# 传输失败重试次数
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒

# adb pull/push 在 Windows 上会把本地路径中的 [ ] 当作 glob 字符类展开，
# 导致含方括号的路径（如某些应用数据目录 [Albums]）无法创建文件。
# 检测到方括号时，先传输到无特殊字符的临时路径，再 shutil.move 到目标。
_GLOB_CHARS = frozenset('[]*?')


def _has_glob_chars(path: str) -> bool:
    """检查路径是否包含 adb 会误解析为 glob 的字符"""
    return any(c in _GLOB_CHARS for c in path)


def _make_temp_path(local_path: str) -> str:
    """在目标同盘上生成无特殊字符的临时文件路径（确保 move 是 rename 而非跨盘复制）"""
    import uuid

    drive, _ = os.path.splitdrive(local_path)
    if drive:
        temp_base = os.path.join(drive, os.sep, '.adb_temp')
    else:
        import tempfile
        temp_base = tempfile.gettempdir()
    os.makedirs(temp_base, exist_ok=True)

    ext = os.path.splitext(local_path)[1]
    return os.path.join(temp_base, f'pull_{uuid.uuid4().hex}{ext}')


def _adb_pull(remote_path: str, local_path: str, serial: str,
              timeout: int = TRANSFER_TIMEOUT) -> None:
    """adb pull：从手机拉取文件到本地

    规范化本地路径，失败时自动重试。
    含 [ ] * ? 等 glob 字符的本地路径先 pull 到临时文件再移动。
    """
    local_path = os.path.normpath(local_path)

    if _has_glob_chars(local_path):
        _adb_pull_via_temp(remote_path, local_path, serial, timeout)
    else:
        _adb_pull_direct(remote_path, local_path, serial, timeout)


def _adb_pull_direct(remote_path: str, local_path: str, serial: str,
                     timeout: int) -> None:
    """直接 adb pull（原始逻辑 + 重试）"""
    # 确保目标目录存在
    target_dir = os.path.dirname(local_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ['adb', '-s', serial, 'pull', remote_path, local_path],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=timeout,
            )
            if result.returncode == 0:
                return
            last_err = result.stderr.strip() or result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = f"超时 ({timeout}s)"
        except FileNotFoundError:
            raise OSError("adb 命令未找到")
        except Exception as e:
            last_err = str(e)

        if attempt < MAX_RETRIES:
            import time
            time.sleep(RETRY_DELAY)

    raise OSError(f"adb pull 失败 (重试 {MAX_RETRIES} 次): {last_err}")


def _adb_pull_via_temp(remote_path: str, local_path: str, serial: str,
                       timeout: int) -> None:
    """通过临时文件中转的 adb pull（处理含 glob 字符的本地路径）

    1. 确保目标目录存在（Python os.makedirs 不受方括号影响）
    2. pull 到同盘临时目录下的无特殊字符文件名
    3. shutil.move 重命名为最终路径（同盘 = rename，瞬间完成）
    """
    import shutil

    # 确保目标目录存在
    target_dir = os.path.dirname(local_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    temp_path = _make_temp_path(local_path)

    try:
        _adb_pull_direct(remote_path, temp_path, serial, timeout)
        shutil.move(temp_path, local_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _adb_push(local_path: str, remote_path: str, serial: str,
              timeout: int = TRANSFER_TIMEOUT) -> None:
    """adb push：从本地推送文件到手机

    规范化本地路径，失败时自动重试。
    含 glob 字符的本地路径先复制到临时文件再推送。
    """
    local_path = os.path.normpath(local_path)

    if _has_glob_chars(local_path):
        _adb_push_via_temp(local_path, remote_path, serial, timeout)
    else:
        _adb_push_direct(local_path, remote_path, serial, timeout)


def _adb_push_direct(local_path: str, remote_path: str, serial: str,
                     timeout: int) -> None:
    """直接 adb push（原始逻辑 + 重试）"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ['adb', '-s', serial, 'push', local_path, remote_path],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=timeout,
            )
            if result.returncode == 0:
                return
            last_err = result.stderr.strip() or result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = f"超时 ({timeout}s)"
        except FileNotFoundError:
            raise OSError("adb 命令未找到")
        except Exception as e:
            last_err = str(e)

        if attempt < MAX_RETRIES:
            import time
            time.sleep(RETRY_DELAY)

    raise OSError(f"adb push 失败 (重试 {MAX_RETRIES} 次): {last_err}")


def _adb_push_via_temp(local_path: str, remote_path: str, serial: str,
                       timeout: int) -> None:
    """通过临时文件中转的 adb push（处理含 glob 字符的本地路径）"""
    import shutil

    temp_path = _make_temp_path(local_path)
    try:
        shutil.copy2(local_path, temp_path)
        _adb_push_direct(temp_path, remote_path, serial, timeout)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ── 统一文件读取操作 ──────────────────────────────────

def scan_files(path: str) -> list[tuple[str, int, float]]:
    """递归扫描目录，返回 [(filepath, size, mtime_epoch), ...]

    本地路径：os.walk() + os.path.getsize/getmtime
    ADB 路径：find -exec stat -c "%n|%s|%Y" 批量获取（~300 文件/秒）
    """
    if is_adb_path(path):
        return _scan_adb(path)
    return _scan_local(path)


def _scan_local(dir_path: str) -> list[tuple[str, int, float]]:
    """本地目录递归扫描"""
    results = []
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                size = os.path.getsize(fp)
                mtime = os.path.getmtime(fp)
                results.append((fp, size, mtime))
            except OSError:
                continue
    return results


def _scan_adb(adb_path: str) -> list[tuple[str, int, float]]:
    """ADB 目录递归扫描，使用 find + stat 批量获取元数据

    对大量文件（如 2 万+），find -exec stat 可能因部分文件权限问题返回非零
    退出码，但 stdout 仍有有效数据。因此用 ; true 保证退出码为 0。
    超时设为 120 秒以应对超大目录。
    """
    serial, android_path = parse_adb_path(adb_path)
    # ; true 确保即使部分 stat 失败也不影响整体退出码
    cmd = f'find "{android_path}" -type f -exec stat -c "%n|%s|%Y" {{}} + 2>/dev/null; true'
    output = _adb_shell(cmd, serial, timeout=120)
    results = []
    for line in output.strip().split('\n'):
        line = line.strip()
        if not line or '|' not in line:
            continue
        # rsplit 从右分割，避免文件名中含 | 时出错
        parts = line.rsplit('|', 2)
        if len(parts) != 3:
            continue
        filepath, size_str, mtime_str = parts
        try:
            size = int(size_str)
            mtime = float(mtime_str)
        except ValueError:
            continue
        # 将 Android 路径转回 adb:// URI
        full_path = build_adb_path(serial, filepath)
        results.append((full_path, size, mtime))
    return results


def exists(path: str) -> bool:
    """文件/目录是否存在（本地 + ADB）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        output = _adb_shell(f'test -e "{android_path}" && echo 1 || echo 0', serial)
        return output.strip() == '1'
    return os.path.exists(path)


def is_file(path: str) -> bool:
    """判断路径是否为文件（本地 + ADB）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        output = _adb_shell(f'test -f "{android_path}" && echo 1 || echo 0', serial)
        return output.strip() == '1'
    return os.path.isfile(path)


def is_dir(path: str) -> bool:
    """判断路径是否为目录（本地 + ADB）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        output = _adb_shell(f'test -d "{android_path}" && echo 1 || echo 0', serial)
        return output.strip() == '1'
    return os.path.isdir(path)


def getsize(path: str) -> int:
    """文件大小（本地 + ADB）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        output = _adb_shell(f'stat -c %s "{android_path}"', serial)
        return int(output.strip())
    return os.path.getsize(path)


def getmtime(path: str) -> float:
    """文件修改时间 epoch（本地 + ADB）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        output = _adb_shell(f'stat -c %Y "{android_path}"', serial)
        return float(output.strip())
    return os.path.getmtime(path)


def list_dir(adb_path: str) -> list[tuple[str, bool]]:
    """列出 ADB 目录内容，返回 [(name, is_dir), ...]

    供手机目录浏览器使用。
    加尾部斜杠让 ls 跟随符号链接（/sdcard → /storage/self/primary）。
    """
    serial, android_path = parse_adb_path(adb_path)
    clean_path = android_path.rstrip('/')
    output = _adb_shell(f'ls -la "{clean_path}/"', serial)
    entries = []
    for line in output.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('total '):
            continue
        # ls -la 格式：perm links owner group size date time name
        # 用 maxsplit=7 分割，第 8 部分是文件名（可能含空格）
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        perm = parts[0]
        name = parts[7]
        is_dir = perm[0] == 'd'
        entries.append((name, is_dir))
    return entries


# ── 统一文件写入操作 ──────────────────────────────────

def makedirs(path: str) -> None:
    """创建目录（本地 os.makedirs / ADB adb shell mkdir -p）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        _adb_shell(f'mkdir -p "{android_path}"', serial)
    else:
        os.makedirs(path, exist_ok=True)


def remove(path: str) -> None:
    """删除文件（本地 os.remove / ADB adb shell rm）"""
    if is_adb_path(path):
        serial, android_path = parse_adb_path(path)
        _adb_shell(f'rm "{android_path}"', serial)
    else:
        os.remove(path)


def copy_file(src: str, dst: str) -> None:
    """复制文件，自动判断方向

    - src=adb://, dst=local → adb pull（手机→电脑）
    - src=local, dst=adb:// → adb push（电脑→手机）
    - both local → shutil.copy2（保留元数据）
    - both adb:// → 不支持，抛出 ValueError
    """
    src_is_adb = is_adb_path(src)
    dst_is_adb = is_adb_path(dst)

    if src_is_adb and dst_is_adb:
        raise ValueError("不支持手机→手机复制（两端都是 ADB 路径）")

    if src_is_adb:
        # pull：手机 → 电脑
        serial, remote_path = parse_adb_path(src)
        _adb_pull(remote_path, dst, serial)
    elif dst_is_adb:
        # push：电脑 → 手机
        serial, remote_path = parse_adb_path(dst)
        _adb_push(src, remote_path, serial)
    else:
        # 本地复制（保留元数据）
        shutil.copy2(src, dst)
