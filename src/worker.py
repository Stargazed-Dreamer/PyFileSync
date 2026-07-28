import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List
from PySide6.QtCore import QThread, Signal

from .models import FileOperation, ResumeState
from . import adb_bridge
from . import logger

class BackupWorker(QThread):
    """备份工作线程
    - 接收操作清单并执行复制/删除
    - 海量小文件复制使用受控并发批次
    - Windows 上可用 RoboCopy 批量处理同目录文件
    - 支持重复文件处理策略：overwrite/skip/check
    - 支持“跳过早于时间戳”的过滤
    - 预留断点续传扩展点（ResumeState）
    """
    progress_updated = Signal(int, int)  # current, total
    operation_updated = Signal(str, str)  # operation, status
    log_message = Signal(str)
    finished = Signal(bool)  # success

    def __init__(self, operations: List[FileOperation], mode: str,
                 skip_older: bool, timestamp: datetime, duplicate_mode: str,
                 resume_state: ResumeState | None = None,
                 start_index: int = 0,
                 copy_workers: int | None = None,
                 copy_backend: str = 'auto'):
        """初始化文件复制操作对象。

        此构造函数设置操作所需的基本参数和内部状态标志。

        参数:
            operations (List[FileOperation]): 要执行的文件操作列表。
            mode (str): 操作模式。
            skip_older (bool): 是否跳过比给定时间戳更旧的文件。
            timestamp (datetime): 用于比较文件修改时间的时间戳。
            duplicate_mode (str): 处理重复文件的策略模式。
            resume_state (ResumeState | None, 可选): 用于断点续传的状态对象，默认为 None。
            start_index (int, 可选): 操作列表的开始索引，默认为 0。
            copy_workers (int | None, 可选): 复制并发数。None 时根据操作规模自动选择。
            copy_backend (str, 可选): 复制后端：auto、python 或 robocopy。
        """
        super().__init__()
        self.operations = operations
        self.mode = mode
        self.skip_older = skip_older
        self.timestamp = timestamp
        self.duplicate_mode = duplicate_mode  # 'overwrite'（覆盖），'skip'（跳过），'check'（检查）
        self.should_stop = False
        self.pause_requested = False
        self.start_index = max(0, start_index)  # 确保起始索引不为负

        # 根据传入的模式字符串，设置对应的布尔标志位
        self.overwrite_mode = (duplicate_mode == 'overwrite')
        self.skip_mode = (duplicate_mode == 'skip')
        self.check_mode = (duplicate_mode == 'check')

        # 初始化断点续传状态（当前版本可能未完全启用）
        self.resume_state = resume_state

        # ADB 路径检测：含 adb:// 路径时强制 Python 后端 + 单线程
        # RoboCopy 和多线程并发不支持 ADB pull/push
        has_adb = any(
            adb_bridge.is_adb_path(op.src_path) or adb_bridge.is_adb_path(op.des_path)
            for op in operations if op.operation in ('copy', 'delete')
        )
        if has_adb:
            copy_backend = 'python'
            copy_workers = 1

        # 海量小文件的瓶颈通常是逐文件的元数据和调度开销，而不是 SSD 带宽。
        # 只对同一规则内连续的复制操作做小批次并发，删除仍保持顺序执行。
        self.copy_workers = self._resolve_copy_workers(copy_workers)
        self.copy_batch_size = max(1, self.copy_workers)
        self.copy_backend_requested = (copy_backend or 'auto').lower()
        self.copy_backend = self._resolve_copy_backend(self.copy_backend_requested)
        self.robocopy_batch_size = 128

    def _resolve_copy_workers(self, requested: int | None) -> int:
        """选择复制并发数，避免大文件和少量文件被并发放大开销。"""
        copy_count = sum(1 for op in self.operations if op.operation == 'copy')
        if requested is not None:
            return max(1, min(int(requested), max(1, copy_count)))

        if copy_count < 8:
            return 1

        copy_ops = [op for op in self.operations if op.operation == 'copy']
        sizes = [op.size for op in copy_ops if op.size > 0]
        if not sizes:
            # 从进度文件恢复时旧格式没有保存 size，少量 stat 足以决定策略。
            for op in copy_ops[:64]:
                try:
                    sizes.append(os.path.getsize(op.src_path))
                except OSError:
                    continue

        if not sizes:
            return 1

        small_file_ratio = sum(size <= 16 * 1024 * 1024 for size in sizes) / len(sizes)
        if small_file_ratio < 0.75 or max(sizes) > 128 * 1024 * 1024:
            return 1

        cpu_count = os.cpu_count() or 4
        return min(8, max(2, cpu_count // 2), copy_count)

    @staticmethod
    def _resolve_copy_backend(requested: str) -> str:
        """选择可用的复制后端，RoboCopy 仅在 Windows 上启用。"""
        if requested not in {'auto', 'python', 'robocopy'}:
            raise ValueError(f"未知复制后端: {requested}")
        if requested == 'python':
            return 'python'
        if os.name == 'nt' and shutil.which('robocopy'):
            return 'robocopy'
        return 'python'

    @staticmethod
    def _format_bytes(size: int) -> str:
        value = float(size)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if value < 1024 or unit == 'GB':
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    def run(self):
        """执行备份操作"""
        total = len(self.operations)
        success_count = 0

        started_at = time.monotonic()
        copy_count = sum(1 for op in self.operations[self.start_index:] if op.operation == 'copy')
        if self.copy_backend_requested == 'robocopy' and self.copy_backend != 'robocopy':
            self.log_message.emit("RoboCopy 不可用，已回退到 Python 复制器")
        self.log_message.emit(
            f"复制执行器: {self.copy_backend}，{self.copy_workers} 个并发工作线程，"
            f"{copy_count} 个复制操作"
        )
        logger.log_session_start(f"备份 (起始索引 {self.start_index})", total - self.start_index)

        executor = (
            ThreadPoolExecutor(max_workers=self.copy_workers)
            if self.copy_backend == 'python' and self.copy_workers > 1
            else None
        )
        try:
            idx = self.start_index
            while idx < total:
                if self.should_stop:
                    self.log_message.emit("操作已取消")
                    logger.log("操作被用户取消")
                    self.finished.emit(False)
                    return

                op = self.operations[idx]
                if op.operation == 'copy' and self.copy_backend == 'robocopy':
                    batch = self._collect_robocopy_batch(idx)
                    success_count += self._run_robocopy_batch(batch, idx, total)
                    idx += len(batch)
                elif op.operation == 'copy' and self.copy_workers > 1:
                    batch = self._collect_copy_batch(idx)
                    success_count += self._run_copy_batch(batch, idx, total, executor)
                    idx += len(batch)
                else:
                    success_count += self._run_single_operation(op, idx, total)
                    idx += 1

                if self.pause_requested:
                    self.log_message.emit("已暂停")
                    logger.log(f"操作已暂停 (完成到索引 {idx}/{total})")
                    # 当前批次已完成，进度索引不会跳过未完成的操作。
                    self.finished.emit(False)
                    return
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        elapsed = max(time.monotonic() - started_at, 0.001)
        copied_bytes = sum(
            op.size for op in self.operations[self.start_index:]
            if op.operation == 'copy' and op.status == 'success'
        )
        throughput = copied_bytes / elapsed
        self.log_message.emit(
            f"操作完成: {success_count}/{total} 成功，耗时 {elapsed:.1f} 秒，"
            f"复制 {self._format_bytes(copied_bytes)} ({self._format_bytes(int(throughput))}/s)"
        )

        # 统计失败和跳过的操作
        processed_ops = self.operations[self.start_index:]
        failed_ops = [op for op in processed_ops if op.status == 'failed']
        skipped_count = sum(1 for op in processed_ops if op.status == 'skipped')
        failed_count = len(failed_ops)
        success_only = success_count - skipped_count

        logger.log_session_end('备份', success_only, failed_count, skipped_count, elapsed)
        logger.log_failed_summary(failed_ops)
        if failed_ops:
            self.log_message.emit(
                f"⚠️ {failed_count} 个操作失败，详情见 backup_log.txt"
            )

        has_failures = any(
            op.status == 'failed' for op in self.operations[self.start_index:]
        )
        self.finished.emit(not has_failures)

    def _collect_robocopy_batch(self, start_index: int) -> List[FileOperation]:
        """收集同一源目录和目标目录下的文件，作为 RoboCopy 的文件参数。"""
        first = self.operations[start_index]
        first_rule = getattr(first, 'rule_index', 0)
        source_dir = os.path.normcase(os.path.abspath(os.path.dirname(first.src_path)))
        target_dir = os.path.normcase(os.path.abspath(os.path.dirname(first.des_path)))
        batch: List[FileOperation] = []
        destinations = set()
        command_length = len(source_dir) + len(target_dir)

        for op in self.operations[start_index:]:
            if op.operation != 'copy' or getattr(op, 'rule_index', 0) != first_rule:
                break
            op_source_dir = os.path.normcase(os.path.abspath(os.path.dirname(op.src_path)))
            op_target_dir = os.path.normcase(os.path.abspath(os.path.dirname(op.des_path)))
            if op_source_dir != source_dir or op_target_dir != target_dir:
                break

            destination = os.path.normcase(os.path.abspath(op.des_path))
            filename = os.path.basename(op.src_path)
            if not filename or destination in destinations:
                break
            if batch and (command_length + len(filename) + 3 > 24000):
                break

            batch.append(op)
            destinations.add(destination)
            command_length += len(filename) + 3
            if len(batch) >= self.robocopy_batch_size:
                break

        return batch

    def _run_robocopy_batch(self, batch: List[FileOperation], start_index: int, total: int) -> int:
        """用 RoboCopy 执行一批明确文件，小批次回退到 Python 以避免进程启动开销。"""
        if len(batch) < 8:
            success_count = 0
            for offset, op in enumerate(batch):
                success_count += self._run_single_operation(op, start_index + offset, total)
            return success_count

        ready: List[FileOperation] = []
        for op in batch:
            if self.skip_older:
                try:
                    if os.path.getmtime(op.src_path) < self.timestamp.timestamp():
                        op.status = 'skipped'
                        self.log_message.emit(f"跳过(早于时间戳): {op.src_path}")
                        continue
                except OSError as e:
                    op.status = 'failed'
                    self.log_message.emit(f"错误: {op.src_path} - {str(e)}")
                    continue
            ready.append(op)

        if ready:
            source_dir = os.path.dirname(os.path.abspath(ready[0].src_path))
            target_dir = os.path.dirname(os.path.abspath(ready[0].des_path))
            filenames = [os.path.basename(op.src_path) for op in ready]
            robocopy_path = shutil.which('robocopy') or 'robocopy'
            command = [
                robocopy_path,
                source_dir,
                target_dir,
                *filenames,
                '/COPY:DAT',
                '/IS',
                '/IT',
                '/R:0',
                '/W:0',
                '/NJH',
                '/NJS',
                '/NDL',
                '/NFL',
                '/NP',
            ]
            if self.copy_workers > 1:
                command.append(f'/MT:{min(self.copy_workers, len(ready))}')

            try:
                result = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                if result.returncode > 7:
                    self.log_message.emit(f"RoboCopy 批次失败，返回码: {result.returncode}")
                    logger.log_error(f"RoboCopy 批次失败 (返回码 {result.returncode}): {source_dir} -> {target_dir}")
                else:
                    self.log_message.emit(
                        f"RoboCopy 批量复制 {len(ready)} 个文件: "
                        f"{source_dir} -> {target_dir}"
                    )

                for op in ready:
                    if result.returncode <= 7 and self._destination_matches(op):
                        op.status = 'success'
                    else:
                        op.status = 'failed'
            except OSError as e:
                self.log_message.emit(f"RoboCopy 启动失败，回退到 Python: {str(e)}")
                logger.log_error("RoboCopy 启动失败，回退到 Python", e)
                self.copy_backend = 'python'
                for op in ready:
                    try:
                        self._copy_file(op)
                    except Exception as copy_error:
                        op.status = 'failed'
                        self.log_message.emit(f"错误: {op.src_path} - {str(copy_error)}")
                        logger.log_error(f"RoboCopy 回退复制失败: {op.src_path}", copy_error)

        success_count = 0
        for offset, op in enumerate(batch):
            self.operation_updated.emit(op.src_path or op.des_path, op.status)
            self.progress_updated.emit(start_index + offset + 1, total)
            if op.status in ['success', 'skipped']:
                success_count += 1
        return success_count

    @staticmethod
    def _destination_matches(op: FileOperation) -> bool:
        """确认 RoboCopy 返回成功后目标文件至少具备完整文件大小。"""
        try:
            return (
                os.path.isfile(op.des_path)
                and os.path.getsize(op.src_path) == os.path.getsize(op.des_path)
            )
        except OSError:
            return False

    def _collect_copy_batch(self, start_index: int) -> List[FileOperation]:
        """收集同一路径规则内的连续复制操作，避免跨删除或跨规则并发。"""
        first_rule = getattr(self.operations[start_index], 'rule_index', 0)
        batch: List[FileOperation] = []
        destinations = set()
        for op in self.operations[start_index:]:
            if op.operation != 'copy' or getattr(op, 'rule_index', 0) != first_rule:
                break
            destination = os.path.normcase(os.path.abspath(op.des_path))
            if destination in destinations:
                break
            batch.append(op)
            destinations.add(destination)
            if len(batch) >= self.copy_batch_size:
                break
        return batch

    def _run_single_operation(self, op: FileOperation, index: int, total: int) -> int:
        """执行一个非并发操作并更新进度。"""
        try:
            if op.operation == 'copy':
                self._copy_file(op)
            elif op.operation == 'delete':
                self._delete_file(op)
        except Exception as e:
            op.status = 'failed'
            err_path = op.src_path or op.des_path
            self.log_message.emit(f"错误: {err_path} - {str(e)}")
            logger.log_error(f"操作失败 [{op.operation}]: {err_path}", e)

        self.operation_updated.emit(op.src_path or op.des_path, op.status)
        self.progress_updated.emit(index + 1, total)
        return 1 if op.status in ['success', 'skipped'] else 0

    def _run_copy_batch(self, batch: List[FileOperation], start_index: int,
                        total: int, executor: ThreadPoolExecutor | None = None) -> int:
        """并发执行一个小批次复制，批次完成后才允许断点继续。"""
        if len(batch) <= 1:
            return self._run_single_operation(batch[0], start_index, total)

        success_count = 0
        owns_executor = executor is None
        pool = executor or ThreadPoolExecutor(max_workers=min(self.copy_workers, len(batch)))
        try:
            futures = {
                pool.submit(self._copy_file, op): op
                for op in batch
            }
            completed = 0
            for future in as_completed(futures):
                op = futures[future]
                try:
                    future.result()
                except Exception as e:
                    op.status = 'failed'
                    err_path = op.src_path or op.des_path
                    self.log_message.emit(f"错误: {err_path} - {str(e)}")
                    logger.log_error(f"并发复制失败: {err_path}", e)

                completed += 1
                if op.status in ['success', 'skipped']:
                    success_count += 1
                self.operation_updated.emit(op.src_path or op.des_path, op.status)
                self.progress_updated.emit(start_index + completed, total)
        finally:
            if owns_executor:
                pool.shutdown(wait=True)

        return success_count

    def _copy_file(self, op: FileOperation):
        """复制文件"""
        # 跳过早于时间戳的文件（仅在配置启用时）
        if self.skip_older:
            src_mtime = adb_bridge.getmtime(op.src_path)
            if src_mtime < self.timestamp.timestamp():
                op.status = 'skipped'
                self.log_message.emit(f"跳过(早于时间戳): {op.src_path}")
                return

        # 执行阶段不再二次判定重复策略（已在预览阶段生成操作清单时处理）

        # 确保目标目录存在
        target_dir = os.path.dirname(op.des_path)
        if target_dir:
            adb_bridge.makedirs(target_dir)

        # 复制文件（保留元数据，自动判断本地/ADB方向）
        adb_bridge.copy_file(op.src_path, op.des_path)
        op.status = 'success'

        # 操作日志显示
        operation_type = "覆盖" if op.is_overwrite else "复制"
        location = "从源" if op.operation_location == 'source' else "目标"
        self.log_message.emit(f"【{operation_type}|{location}】 {op.src_path} -> {op.des_path}")

    def _delete_file(self, op: FileOperation):
        """删除文件（按操作清单执行）"""
        adb_bridge.remove(op.des_path)
        op.status = 'success'
        self.log_message.emit(f"【删除|目标】 {op.des_path}")

    def stop(self):
        """停止操作"""
        self.should_stop = True

    def pause(self):
        """请求暂停（当前并发批次完成后暂停）"""
        self.pause_requested = True

    # 预留：断点续传检查点更新
    # def _update_checkpoint(self, op: FileOperation):
    #     """更新检查点文件，记录已完成操作（未来启用）"""
    #     pass
