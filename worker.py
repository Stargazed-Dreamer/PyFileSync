import os
import shutil
from datetime import datetime
from typing import List
from PySide6.QtCore import QThread, Signal

from models import FileOperation, ResumeState

class BackupWorker(QThread):
    """备份工作线程
    - 接收操作清单并按顺序执行
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
                 start_index: int = 0):
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

    def run(self):
        """执行备份操作"""
        total = len(self.operations)
        success_count = 0

        for idx in range(self.start_index, len(self.operations)):
            op = self.operations[idx]
            if self.should_stop:
                self.log_message.emit("操作已取消")
                self.finished.emit(False)
                return

            self.progress_updated.emit(idx + 1, total)

            try:
                if op.operation == 'copy':
                    self._copy_file(op)
                elif op.operation == 'delete':
                    self._delete_file(op)

                if op.status in ['success', 'skipped']:
                    success_count += 1
                    # 未来：在此更新断点续传检查点
                    # if self.resume_state and self.resume_state.enabled:
                    #     self._update_checkpoint(op)

            except Exception as e:
                op.status = 'failed'
                self.log_message.emit(f"错误: {op.src_path} - {str(e)}")

            self.operation_updated.emit(op.src_path or op.des_path, op.status)

            if self.pause_requested:
                self.log_message.emit("已暂停")
                # 结束线程但标记未完成，由上层写入进度
                self.finished.emit(False)
                return

        self.log_message.emit(f"操作完成: {success_count}/{total} 成功")
        self.finished.emit(True)

    def _copy_file(self, op: FileOperation):
        """复制文件"""
        # 跳过早于时间戳的文件（仅在配置启用时）
        if self.skip_older:
            src_mtime = os.path.getmtime(op.src_path)
            if src_mtime < self.timestamp.timestamp():
                op.status = 'skipped'
                self.log_message.emit(f"跳过(早于时间戳): {op.src_path}")
                return

        # 执行阶段不再二次判定重复策略（已在预览阶段生成操作清单时处理）

        # 确保目标目录存在
        os.makedirs(os.path.dirname(op.des_path), exist_ok=True)

        # 复制文件（保留元数据）
        shutil.copy2(op.src_path, op.des_path)
        op.status = 'success'

        # 操作日志显示
        operation_type = "覆盖" if op.is_overwrite else "复制"
        location = "从源" if op.operation_location == 'source' else "目标"
        self.log_message.emit(f"【{operation_type}|{location}】 {op.src_path} -> {op.des_path}")

    def _delete_file(self, op: FileOperation):
        """删除文件（按操作清单执行）"""
        os.remove(op.des_path)
        op.status = 'success'
        self.log_message.emit(f"【删除|目标】 {op.des_path}")

    def stop(self):
        """停止操作"""
        self.should_stop = True

    def pause(self):
        """请求暂停（在当前文件完成后暂停）"""
        self.pause_requested = True

    # 预留：断点续传检查点更新
    # def _update_checkpoint(self, op: FileOperation):
    #     """更新检查点文件，记录已完成操作（未来启用）"""
    #     pass