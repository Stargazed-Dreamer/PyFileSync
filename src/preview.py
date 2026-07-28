import os
import posixpath
from typing import List, Callable, Optional, Tuple

from PySide6.QtCore import QThread, Signal

from .models import FileOperation, PathRule
from . import adb_bridge
from . import logger


class PreviewWorker(QThread):
    """后台预览线程，避免大量文件扫描阻塞 UI

    用法：
        worker = PreviewWorker(preview_manager, rules)
        worker.logSignal.connect(self.log_message)
        worker.finishedSignal.connect(self._on_preview_done)
        worker.start()
    """
    logSignal = Signal(str)
    finishedSignal = Signal(list)  # List[FileOperation]

    def __init__(self, preview_manager: 'PreviewManager', rules: List[PathRule]):
        super().__init__()
        self._pm = preview_manager
        self._rules = rules

    def run(self):
        try:
            # 临时将日志回调指向信号，线程安全
            orig_cb = self._pm.log_callback
            self._pm.log_callback = lambda msg: self.logSignal.emit(msg)
            ops = self._pm.preview_operations(self._rules)
            self._pm.log_callback = orig_cb
            self.finishedSignal.emit(ops)
        except Exception as e:
            self.logSignal.emit(f"❌ 预览出错: {e}")
            logger.log_error("预览线程异常退出", e)
            self.finishedSignal.emit([])


class PreviewManager:
    """预览管理器，负责处理文件操作的预览逻辑"""
    
    def __init__(self, log_callback: Optional[Callable[[str], None]] = None):
        """
        初始化日志记录器实例。

        Args:
            log_callback (Optional[Callable[[str], None]]): 可选的日志记录回调函数。
                如果未提供，则默认使用一个不执行任何操作的lambda函数。

        Returns:
            None
        """
        # 如果log_callback参数为空（None），则使用一个默认的、不执行任何操作的lambda函数作为后备
        self.log_callback = log_callback or (lambda msg: None)
        self.operations: List[FileOperation] = []  # 用于存储待执行或已执行的文件操作记录
    
    def log(self, message: str):
        """记录日志"""
        self.log_callback(f"[预览] {message}")

    @staticmethod
    def _compute_rel_path(filepath: str, base_dir: str) -> str:
        """计算文件相对于基础目录的相对路径，支持 ADB 路径

        返回统一使用 / 分隔符（本地路径后续 os.path.join 能正确处理）
        """
        if adb_bridge.is_adb_path(base_dir):
            _, android_base = adb_bridge.parse_adb_path(base_dir)
            _, android_file = adb_bridge.parse_adb_path(filepath)
            return posixpath.relpath(android_file, android_base)
        return os.path.relpath(filepath, base_dir).replace('\\', '/')

    @staticmethod
    def _build_path(base_dir: str, rel_path: str) -> str:
        """拼接基础目录和相对路径，支持 ADB 路径

        ADB 路径用 posixpath.join（正斜杠），本地路径用 os.path.join
        """
        rel_path = rel_path.replace('\\', '/')
        if adb_bridge.is_adb_path(base_dir):
            serial, android_base = adb_bridge.parse_adb_path(base_dir)
            android_path = posixpath.join(android_base, rel_path)
            return adb_bridge.build_adb_path(serial, android_path)
        return os.path.normpath(os.path.join(base_dir, rel_path))
    
    def preview_operations(self, path_rules: List[PathRule]) -> List[FileOperation]:
        """
        预览所有路径规则的操作
        
        Args:
            path_rules: 路径规则列表
            
        Returns:
            操作列表
        """
        self.log(f"开始预览操作，共 {len(path_rules)} 个路径规则")
        logger.log_session_start('预览', len(path_rules))
        self.operations = []
        
        # 保存路径规则信息，用于后续的显示分组
        self._current_rules = path_rules
        
        for i, rule in enumerate(path_rules):
            self.log(f"处理路径规则 {i+1}/{len(path_rules)}: {rule.src_dir} -> {rule.des_dir}")
            
            try:
                if not adb_bridge.exists(rule.src_dir):
                    self.log(f"⚠️ 源路径不存在，跳过: {rule.src_dir}")
                    logger.log(f"源路径不存在，跳过: {rule.src_dir}", 'WARNING')
                    continue
                
                if adb_bridge.is_file(rule.src_dir):
                    self.log(f"处理单个文件: {rule.src_dir}")
                    self._process_single_file(rule, rule.des_dir, i)
                elif adb_bridge.is_dir(rule.src_dir):
                    self.log(f"处理文件夹: {rule.src_dir}")
                    self._process_directory(rule, rule.des_dir, i)
                else:
                    self.log(f"❌ 未知的源路径类型: {rule.src_dir}")
                    logger.log(f"未知源路径类型: {rule.src_dir}", 'WARNING')
            except Exception as e:
                # 单规则失败不中止整个预览
                self.log(f"❌ 处理规则 {i+1} 时出错: {e}，跳过此规则")
                logger.log_error(f"预览规则 {i+1} 出错 ({rule.src_dir} -> {rule.des_dir})", e)
        
        # 统计操作数量
        copy_count = sum(1 for op in self.operations if op.operation == 'copy')
        delete_count = sum(1 for op in self.operations if op.operation == 'delete')
        overwrite_count = sum(1 for op in self.operations if op.operation == 'copy' and op.is_overwrite)
        
        self.log(f"预览完成！总操作数: {len(self.operations)}")
        self.log(f"  - 新复制: {copy_count - overwrite_count} 个文件")
        self.log(f"  - 覆盖更新: {overwrite_count} 个文件")
        self.log(f"  - 删除: {delete_count} 个文件")
        
        logger.log_session_end('预览', copy_count - overwrite_count + delete_count, 0, overwrite_count, 0)
        
        return self.operations
    
    def _process_single_file(self, rule: PathRule, des_path: str, rule_index: int):
        """
        处理单个文件的复制操作
        
        Args:
            rule: 路径规则
            des_path: 目标路径
            rule_index: 规则索引
        """
        src_file = rule.src_dir
        filename = os.path.basename(src_file)
        
        # 确定目标文件路径
        if adb_bridge.is_dir(des_path):
            des_file = self._build_path(des_path, filename)
            self.log(f"  目标为文件夹，目标文件: {des_file}")
        else:
            des_file = des_path
            self.log(f"  目标为文件路径: {des_file}")
        
        # 检查过滤条件
        rel_path = filename
        if self._is_filtered_out(rel_path, rule.excludes, rule.includes, src_file):
            self.log(f"  🚫 文件被过滤掉: {rel_path}")
            return
        
        # 检查是否需要复制
        if self._should_copy_file(src_file, des_file, rule.duplicate_mode):
            is_overwrite = adb_bridge.exists(des_file)
            file_size = adb_bridge.getsize(src_file)
            
            operation_desc = "覆盖" if is_overwrite else "新建"
            #self.log(f"  ➕ {operation_desc}文件: {filename} ({file_size} 字节)")
            
            op = FileOperation(
                'copy', src_file, des_file, file_size,
                is_overwrite=is_overwrite, operation_location='source', rule_index=rule_index
            )
            self.operations.append(op)
        else:
            #self.log(f"  ⏭️ 跳过文件: {filename} (无需更新)")
            pass
    
    def _process_directory(self, rule: PathRule, des_dir: str, rule_index: int):
        """
        处理文件夹的复制和删除操作

        性能关键：源和目标都批量扫描一次（各一次 ADB 调用），
        之后全部在内存中比较，避免逐文件 ADB shell 调用。
        """
        src_dir = rule.src_dir
        self.log(f"  开始遍历源文件夹: {src_dir}")

        files_processed = 0
        files_filtered = 0
        files_to_copy = 0

        # 批量扫描源目录（一次 ADB 调用 / 一次 os.walk）
        all_files = adb_bridge.scan_files(src_dir)
        self.log(f"    扫描到 {len(all_files)} 个源文件")

        # 批量扫描目标目录，构建 {rel_path: (size, mtime)} 字典（一次调用）
        des_files_map: dict[str, tuple[int, float]] = {}
        if adb_bridge.exists(des_dir):
            des_files = adb_bridge.scan_files(des_dir)
            for des_file, des_size, des_mtime in des_files:
                rel = self._compute_rel_path(des_file, des_dir)
                des_files_map[rel] = (des_size, des_mtime)
            self.log(f"    扫描到 {len(des_files_map)} 个目标文件")

        # 构建源文件相对路径集合（供完全同步删除检查用）
        src_rel_paths: set[str] = set()

        for src_file, file_size, file_mtime in all_files:
            files_processed += 1
            rel_path = self._compute_rel_path(src_file, src_dir)
            src_rel_paths.add(rel_path)
            des_file = self._build_path(des_dir, rel_path)

            # 检查过滤条件
            if self._is_filtered_out(rel_path, rule.excludes, rule.includes, src_file):
                files_filtered += 1
                if files_filtered <= 5:
                    self.log(f"      🚫 过滤文件: {rel_path}")
                elif files_filtered == 6:
                    self.log(f"      ... (更多被过滤的文件将不再显示)")
                continue

            # 从字典中获取目标文件元数据（内存查询，零 ADB 调用）
            des_meta = des_files_map.get(rel_path)
            if des_meta is not None:
                des_exists = True
                des_size, des_mtime = des_meta
            else:
                des_exists = False
                des_size, des_mtime = None, None

            # 检查是否需要复制（全部用预取的元数据，无 ADB 调用）
            if self._should_copy_file(
                src_file, des_file, rule.duplicate_mode,
                src_size=file_size, src_mtime=file_mtime,
                des_exists=des_exists, des_size=des_size, des_mtime=des_mtime
            ):
                is_overwrite = des_exists
                files_to_copy += 1

                op = FileOperation(
                    'copy', src_file, des_file, file_size,
                    is_overwrite=is_overwrite, operation_location='source', rule_index=rule_index
                )
                self.operations.append(op)

        self.log(f"  源文件夹扫描完成: {files_processed} 个文件，过滤 {files_filtered} 个，需复制 {files_to_copy} 个")

        # 如果是完全同步模式，处理目标文件夹中多余的文件
        if getattr(rule, 'change_mode', 'incremental') == 'sync':
            self.log(f"  完全同步模式：检查目标文件夹中多余的文件")
            self._process_sync_deletions(rule, src_dir, des_dir, rule_index, src_rel_paths)
        else:
            self.log(f"  增量更新模式：跳过删除检查")
    
    def _process_sync_deletions(self, rule: PathRule, src_dir: str, des_dir: str,
                                rule_index: int, src_rel_paths: set[str] | None = None):
        """
        处理完全同步模式下的删除操作

        src_rel_paths 为源文件相对路径集合，提供时用内存查询替代逐文件 exists。
        """
        if not adb_bridge.exists(des_dir):
            self.log(f"    目标文件夹不存在，无需删除: {des_dir}")
            return

        files_to_delete = 0
        files_filtered = 0

        for des_file, file_size, file_mtime in adb_bridge.scan_files(des_dir):
            rel_path = self._compute_rel_path(des_file, des_dir)

            # 用预建的源路径集合判断（内存查询），无则回退到 exists
            if src_rel_paths is not None:
                src_exists = rel_path in src_rel_paths
            else:
                src_file = self._build_path(src_dir, rel_path)
                src_exists = adb_bridge.exists(src_file)

            if not src_exists:
                rel_path_filtered = self._is_filtered_out(rel_path, rule.excludes, rule.includes)
                src_file = self._build_path(src_dir, rel_path)
                src_path_filtered = self._is_filtered_out("", rule.excludes, rule.includes, src_file)

                if rel_path_filtered or src_path_filtered:
                    files_filtered += 1
                    if files_filtered <= 3:
                        self.log(f"      🚫 跳过删除被过滤的文件: {rel_path}")
                    continue

                self.log(f"      🗑️ 删除多余文件: {rel_path} ({file_size} 字节)")
                files_to_delete += 1

                op = FileOperation(
                    'delete', '', des_file, file_size,
                    operation_location='target', rule_index=rule_index
                )
                self.operations.append(op)

        self.log(f"  同步删除检查完成: 需删除 {files_to_delete} 个文件，跳过 {files_filtered} 个被过滤的文件")
    
    def _should_copy_file(self, src_file: str, des_file: str, duplicate_mode: str,
                          src_size: int | None = None, src_mtime: float | None = None,
                          des_exists: bool | None = None,
                          des_size: int | None = None, des_mtime: float | None = None) -> bool:
        """
        判断文件是否需要复制

        批量模式下 src_size/src_mtime/des_exists/des_size/des_mtime 由调用方预取，
        避免逐文件 ADB shell 调用。单文件模式下（参数为 None）回退到逐个查询。
        """
        # 获取目标文件是否存在
        if des_exists is None:
            des_exists = adb_bridge.exists(des_file)

        if des_exists:
            if duplicate_mode == 'skip':
                return False
            elif duplicate_mode == 'overwrite':
                return True
            elif duplicate_mode == 'check':
                try:
                    if src_mtime is None:
                        src_mtime = adb_bridge.getmtime(src_file)
                    if des_mtime is None:
                        des_mtime = adb_bridge.getmtime(des_file)
                    if src_size is None:
                        src_size = adb_bridge.getsize(src_file)
                    if des_size is None:
                        des_size = adb_bridge.getsize(des_file)

                    src_mtime_i = int(src_mtime)
                    des_mtime_i = int(des_mtime)

                    if src_mtime_i == des_mtime_i and src_size == des_size:
                        return False
                    else:
                        result = False
                        time_diff = abs(src_mtime_i - des_mtime_i)
                        size_diff = abs(src_size - des_size)
                        if time_diff > 10:
                            result = True
                        if size_diff != 0:
                            result = True
                        return result
                except OSError as e:
                    self.log(f"        ⚠️ 检查文件时出错，强制复制: {e}")
                    return True
        return True
    
    def _is_filtered_out(self, rel_path: str, excludes: List[str], includes: List[str], src_file: str = None) -> bool:
        """
        检查文件是否被过滤掉
        
        Args:
            rel_path: 相对路径
            excludes: 排除列表
            includes: 包含列表
            src_file: 源文件的完整路径（用于绝对路径匹配）
            
        Returns:
            是否被过滤掉
        """
        if not excludes and not includes:
            return False  # 如果没有过滤规则，不过滤
            
        # 标准化路径格式
        rp = rel_path.replace('\\', '/').lower()
        ex_norm = [p.replace('\\', '/').lower() for p in excludes]
        inc_norm = [p.replace('\\', '/').lower() for p in includes]
        
        # 首先检查是否在包含列表中（包含列表优先级更高）
        for inc in inc_norm:
            # 相对路径匹配
            if rp == inc or rp.startswith(inc.rstrip('/') + '/'):
                return False
            
            # 绝对路径匹配（如果提供了源文件完整路径）
            if src_file and (':' in inc or inc.startswith('/') or inc.startswith('\\')):
                src_normalized = src_file.replace('\\', '/').lower()
                if src_normalized == inc or src_normalized.startswith(inc.rstrip('/') + '/'):
                    return False
        
        # 然后检查是否在排除列表中
        for ex in ex_norm:
            # 相对路径匹配
            if rp == ex or rp.startswith(ex.rstrip('/') + '/'):
                return True
            
            # 绝对路径匹配（如果提供了源文件完整路径）
            if src_file and (':' in ex or ex.startswith('/') or ex.startswith('\\')):
                src_normalized = src_file.replace('\\', '/').lower()
                if src_normalized == ex or src_normalized.startswith(ex.rstrip('/') + '/'):
                    return True
        
        return False
    
    def get_operation_summary(self) -> str:
        """
        获取操作摘要信息
        
        Returns:
            操作摘要字符串
        """
        if not self.operations:
            return "无操作"
        
        copy_count = sum(1 for op in self.operations if op.operation == 'copy')
        delete_count = sum(1 for op in self.operations if op.operation == 'delete')
        overwrite_count = sum(1 for op in self.operations if op.operation == 'copy' and op.is_overwrite)
        
        summary = []
        summary.append(f"总操作数: {len(self.operations)}")
        summary.append(f"复制: {copy_count} (新建: {copy_count - overwrite_count}, 覆盖: {overwrite_count})")
        summary.append(f"删除: {delete_count}")
        
        return "\n".join(summary)
    
    def get_merged_operation_display(self) -> List[str]:
        """
        获取合并后的操作显示列表
        使用文件合并算法减少输出行数
        
        Returns:
            合并后的操作显示列表
        """
        if not self.operations:
            return []
        
        # 按路径对分组操作
        operation_groups = self._group_operations_by_path_pair()
        merged_display = []
        
        for path_pair_info, ops in operation_groups.items():
            src_base, des_base, rule = path_pair_info
            pair_display = f"路径对: {src_base} -> {des_base}"
            merged_display.append(pair_display)
            
            # 按操作类型分组
            copy_ops = [op for op in ops if op.operation == 'copy']
            delete_ops = [op for op in ops if op.operation == 'delete']
            
            # 处理复制操作
            if copy_ops:
                copy_display = self._merge_file_operations(copy_ops, src_base, 'copy')
                merged_display.extend(copy_display)
            
            # 处理删除操作
            if delete_ops:
                delete_display = self._merge_file_operations(delete_ops, des_base, 'delete')
                merged_display.extend(delete_display)
            
            merged_display.append("")  # 空行分隔不同路径对
        
        return merged_display
    
    def _group_operations_by_path_pair(self) -> dict:
        """
        按路径对分组操作
        
        Returns:
            分组后的操作字典
        """
        groups = {}
        
        for op in self.operations:
            # 获取操作的路径规则信息
            rule_index = getattr(op, 'rule_index', 0)
            
            # 从预览时保存的路径规则中获取基础路径
            if hasattr(self, '_current_rules') and rule_index < len(self._current_rules):
                rule = self._current_rules[rule_index]
                src_base = rule.src_dir
                des_base = rule.des_dir
            else:
                # 回退到原来的逻辑
                if op.operation == 'copy':
                    src_base = self._find_path_pair_base(op.src_path)
                    des_base = self._find_path_pair_base(op.des_path)
                elif op.operation == 'delete':
                    # 对于删除操作，需要反向查找路径对
                    des_base = self._find_path_pair_base(op.des_path)
                    src_base = self._find_source_base_for_delete(op.des_path)
            
            # 使用路径对作为分组键，同时存储规则信息用于容忍度计算
            pair_key = (src_base, des_base, rule_index)
            
            if pair_key not in groups:
                groups[pair_key] = []
            groups[pair_key].append(op)
        
        return groups
    
    def _find_path_pair_base(self, file_path: str) -> str:
        """
        查找文件对应的路径对基础路径
        
        Args:
            file_path: 文件路径
           
        Returns:
            路径对基础路径
        """
        # 这里简化处理，实际应该从路径规则中查找
        # 暂时返回文件的父目录作为基础路径
        import os
        return os.path.dirname(file_path)
    
    def _find_source_base_for_delete(self, des_path: str) -> str:
        """为删除操作找到对应的源基础路径（回退方案）

        优先从已保存的路径规则中匹配，找不到则返回目标父目录。
        不使用 os.walk（对 ADB 路径不可用且极慢）。
        """
        # 尝试从已保存的路径规则中匹配
        if hasattr(self, '_current_rules'):
            for rule in self._current_rules:
                if des_path.startswith(rule.des_dir):
                    return rule.src_dir
        
        # 回退：返回目标路径的父目录
        return os.path.dirname(des_path)
    
    def _merge_file_operations(self, operations: List[FileOperation], base_path: str, operation_type: str) -> List[str]:
        """
        合并文件操作显示
        
        Args:
            operations: 操作列表
            base_path: 基础路径
            operation_type: 操作类型 ('copy' 或 'delete')
            
        Returns:
            合并后的显示列表
        """
        # 构建文件树结构
        file_tree = self._build_file_tree(operations, base_path, operation_type)
        
        # 计算容忍度并合并
        merged_tree = self._apply_tolerance_merge(file_tree, base_tolerance=10)
        
        # 生成显示内容
        return self._generate_merged_display(merged_tree, operation_type)
    
    def _build_file_tree(self, operations: List[FileOperation], base_path: str, operation_type: str) -> dict:
        """
        构建文件树结构
        
        Args:
            operations: 操作列表
            base_path: 基础路径
            operation_type: 操作类型
            
        Returns:
            文件树字典
        """
        file_tree = {}
        
        for op in operations:
            if operation_type == 'copy':
                rel_path = os.path.relpath(op.src_path, base_path)
            else:  # delete
                rel_path = os.path.relpath(op.des_path, base_path)
            
            # 分割路径
            parts = rel_path.replace('\\', '/').split('/')
            current = file_tree
            
            for part in parts[:-1]:  # 遍历目录部分
                if part not in current:
                    current[part] = {'_type': 'dir', '_children': {}}
                current = current[part]['_children']
            
            # 添加文件
            file_name = parts[-1]
            current[file_name] = {
                '_type': 'file',
                '_operation': op,
                '_is_overwrite': getattr(op, 'is_overwrite', False)
            }
        
        return file_tree
    
    def _apply_tolerance_merge(self, file_tree: dict, base_tolerance: int) -> dict:
        """
        应用容忍度合并算法
        
        Args:
            file_tree: 文件树
            base_tolerance: 基础容忍度
            
        Returns:
            合并后的文件树
        """
        def calculate_tolerance(depth: int) -> int:
            """计算指定深度的容忍度"""
            tolerance = base_tolerance
            for _ in range(depth):
                tolerance = int(tolerance * 1.5)
            return tolerance
        
        def merge_at_level(tree: dict, current_depth: int, max_tolerance: int) -> dict:
            """在指定层级进行合并"""
            tolerance = calculate_tolerance(current_depth)
            
            # 检查当前级别的文件数量
            file_count = sum(1 for item in tree.values() if item.get('_type') == 'file')
            
            if file_count > tolerance and current_depth > 0:  # 不能合并根级别
                # 创建合并节点
                merged_count = len(tree)
                return {
                    '_type': 'merged',
                    '_depth': current_depth,
                    '_file_count': file_count,
                    '_total_count': merged_count,
                    '_children': tree
                }
            else:
                # 递归处理子目录
                result = {}
                for name, item in tree.items():
                    if item.get('_type') == 'dir' and '_children' in item:
                        merged_child = merge_at_level(item['_children'], current_depth + 1, max_tolerance)
                        if merged_child:
                            result[name] = {
                                '_type': 'dir',
                                '_children': merged_child
                            }
                    else:
                        result[name] = item
                return result
        
        # 从第一层开始合并
        return merge_at_level(file_tree, 0, base_tolerance)
    
    def _generate_merged_display(self, merged_tree: dict, operation_type: str) -> List[str]:
        """
        生成合并后的显示内容
        
        Args:
            merged_tree: 合并后的文件树
            operation_type: 操作类型
            
        Returns:
            显示内容列表
        """
        display_lines = []
        
        def generate_display(tree: dict, prefix: str = ""):
            if not tree or not isinstance(tree, dict):
                return
                
            for name, item in tree.items():
                # 跳过无效项
                if not isinstance(item, dict):
                    continue
                
                item_type = item.get('_type')
                if item_type == 'merged':
                    # 合并节点
                    file_count = item.get('_file_count', 0)
                    total_count = item.get('_total_count', 0)
                    depth = item.get('_depth', 0)
                    
                    if operation_type == 'copy':
                        overwrite_count = sum(1 for op in self._extract_operations_from_tree(item.get('_children', {})) 
                                              if getattr(op, 'is_overwrite', False))
                        new_count = file_count - overwrite_count
                        display_lines.append(f"{prefix}📁 {name}/ [已合并] 新建:{new_count} 覆盖:{overwrite_count} 共:{file_count} 项")
                    else:
                        display_lines.append(f"{prefix}🗑️ {name}/ [已合并] 删除:{file_count} 项")
                    
                elif item_type == 'dir':
                    # 目录节点
                    display_lines.append(f"{prefix}📁 {name}/")
                    children = item.get('_children', {})
                    if isinstance(children, dict) and children:
                        generate_display(children, prefix + "  ")
                    
                elif item_type == 'file':
                    # 文件节点
                    is_overwrite = item.get('_is_overwrite', False)
                    op = item.get('_operation')
                    
                    if operation_type == 'copy':
                        if op:
                            file_path = op.src_path or ""
                            operation_desc = "覆盖" if is_overwrite else "新建"
                            display_lines.append(f"{prefix}📄{operation_desc} {name}")
                    else:
                        if op:
                            display_lines.append(f"{prefix}🗑️ {name}")
        
        generate_display(merged_tree)
        return display_lines
    
    def _extract_operations_from_tree(self, tree: dict) -> List[FileOperation]:
        """
        从文件树中提取所有操作
        
        Args:
            tree: 文件树
            
        Returns:
            操作列表
        """
        operations = []
        
        def extract(tree_dict):
            for item in tree_dict.values():
                if item.get('_type') == 'file' and '_operation' in item:
                    operations.append(item['_operation'])
                elif item.get('_type') == 'dir' and '_children' in item:
                    extract(item['_children'])
        
        extract(tree)
        return operations
