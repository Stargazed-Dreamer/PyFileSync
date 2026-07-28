"""持久化日志模块

将操作日志和错误信息写入文件，便于事后排查。
线程安全（worker 线程可安全调用），自动轮转防止无限增长。

日志文件位于项目目录下的 backup_log.txt，超过 MAX_LOG_SIZE 时自动截断。
"""

import os
import threading
from datetime import datetime

from . import PROJECT_ROOT

# 日志文件路径（项目根目录下的 backup_log.txt）
LOG_FILE = os.path.join(PROJECT_ROOT, 'backup_log.txt')

# 日志文件最大大小（1MB），超过后截断保留最后 256KB
MAX_LOG_SIZE = 1024 * 1024
TRUNCATE_KEEP = 256 * 1024

# 线程锁
_lock = threading.Lock()


def _truncate_if_needed():
    """日志文件过大时截断，保留最后部分"""
    try:
        size = os.path.getsize(LOG_FILE)
        if size <= MAX_LOG_SIZE:
            return
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(max(0, size - TRUNCATE_KEEP))
            f.readline()  # 跳过可能截断的半行
            remaining = f.read()
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SYSTEM] 日志已截断（原大小 {size // 1024}KB）\n")
            f.write(remaining)
    except OSError:
        pass


def log(message: str, level: str = 'INFO'):
    """写入一条日志到文件

    Args:
        message: 日志内容
        level: 日志级别 INFO / WARNING / ERROR
    """
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] [{level}] {message}\n"
    with _lock:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
            _truncate_if_needed()
        except OSError:
            pass  # 日志写入失败不应影响主流程


def log_error(message: str, exc: Exception | None = None):
    """写入错误日志，附带异常信息

    Args:
        message: 错误描述
        exc: 异常对象（可选）
    """
    if exc is not None:
        import traceback
        tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log(f"{message}\n{tb}", level='ERROR')
    else:
        log(message, level='ERROR')


def log_session_start(task: str, total: int):
    """记录会话开始"""
    log(f"{'=' * 60}")
    log(f"会话开始: {task}，共 {total} 个操作")


def log_session_end(task: str, success: int, failed: int, skipped: int, elapsed: float):
    """记录会话结束"""
    log(f"会话结束: {task} | 成功 {success} | 失败 {failed} | 跳过 {skipped} | 耗时 {elapsed:.1f}s")
    if failed > 0:
        log(f"⚠️ 有 {failed} 个操作失败，请查看上方 ERROR 日志", level='WARNING')
    log(f"{'=' * 60}")


def log_failed_summary(failed_ops: list):
    """输出失败操作清单

    Args:
        failed_ops: 失败的 FileOperation 列表
    """
    if not failed_ops:
        return
    log(f"失败操作清单（共 {len(failed_ops)} 个）:")
    for i, op in enumerate(failed_ops):
        path = op.src_path or op.des_path
        log(f"  [{i + 1}] {op.operation}: {path} - {op.status}")


def get_log_path() -> str:
    """返回日志文件路径"""
    return LOG_FILE
