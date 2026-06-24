import os
from typing import List, Callable, Optional, Tuple

from models import FileOperation, PathRule


class PreviewManager:
    """预览管理器，负责处理文件操作的预览逻辑"""
    
    def __init__(self, log_callback: Optional[Callable[[str], None]] = None):
        self.log_callback = log_callback or (lambda msg: None)
        self.operations: List[FileOperation] = []
    
    def log(self, message: str):
        """记录日志"""
        self.log_callback(f"[预览] {message}")
    
    def preview_operations(self, path_rules: List[PathRule]) -> List[FileOperation]:
        """
        预览所有路径规则的操作
        
        Args:
            path_rules: 路径规则列表
            
        Returns:
            操作列表
        """
        self.log(f"开始预览操作，共 {len(path_rules)} 个路径规则")
        self.operations = []
        
        # 保存路径规则信息，用于后续的显示分组
        self._current_rules = path_rules
        
        for i, rule in enumerate(path_rules):
            self.log(f"处理路径规则 {i+1}/{len(path_rules)}: {rule.src_dir} -> {rule.des_dir}")
            
            if not os.path.exists(rule.src_dir):
                self.log(f"⚠️ 源路径不存在，跳过: {rule.src_dir}")
                continue
            
            if os.path.isfile(rule.src_dir):
                self.log(f"处理单个文件: {rule.src_dir}")
                self._process_single_file(rule, rule.des_dir, i)
            elif os.path.isdir(rule.src_dir):
                self.log(f"处理文件夹: {rule.src_dir}")
                self._process_directory(rule, rule.des_dir, i)
            else:
                self.log(f"❌ 未知的源路径类型: {rule.src_dir}")
        
        # 统计操作数量
        copy_count = sum(1 for op in self.operations if op.operation == 'copy')
        delete_count = sum(1 for op in self.operations if op.operation == 'delete')
        overwrite_count = sum(1 for op in self.operations if op.operation == 'copy' and op.is_overwrite)
        
        self.log(f"预览完成！总操作数: {len(self.operations)}")
        self.log(f"  - 新复制: {copy_count - overwrite_count} 个文件")
        self.log(f"  - 覆盖更新: {overwrite_count} 个文件")
        self.log(f"  - 删除: {delete_count} 个文件")
        
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
        if os.path.isdir(des_path):
            des_file = os.path.join(des_path, filename)
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
            is_overwrite = os.path.exists(des_file)
            file_size = os.path.getsize(src_file)
            
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
        
        Args:
            rule: 路径规则
            des_dir: 目标文件夹路径
            rule_index: 规则索引
        """
        src_dir = rule.src_dir
        self.log(f"  开始遍历源文件夹: {src_dir}")
        
        # 统计处理的文件数量
        files_processed = 0
        files_filtered = 0
        files_to_copy = 0
        
        # 处理源文件夹中的所有文件
        for root, dirs, files in os.walk(src_dir):
            # 记录当前处理的相对路径
            rel_root = os.path.relpath(root, src_dir)
            if rel_root == '.':
                rel_root = ''
            
            self.log(f"    扫描文件夹: {rel_root if rel_root else '(根目录)'} - {len(files)} 个文件")
            
            for file in files:
                files_processed += 1
                src_file = os.path.join(root, file)
                rel_path = os.path.relpath(src_file, src_dir)
                des_file = os.path.join(des_dir, rel_path)
                
                # 检查过滤条件
                if self._is_filtered_out(rel_path, rule.excludes, rule.includes, src_file):
                    files_filtered += 1
                    if files_filtered <= 5:  # 只显示前5个被过滤的文件
                        self.log(f"      🚫 过滤文件: {rel_path}")
                    elif files_filtered == 6:
                        self.log(f"      ... (更多被过滤的文件将不再显示)")
                    continue
                
                # 检查是否需要复制
                if self._should_copy_file(src_file, des_file, rule.duplicate_mode):
                    is_overwrite = os.path.exists(des_file)
                    file_size = os.path.getsize(src_file)
                    
                    operation_desc = "覆盖" if is_overwrite else "新建"
                    #self.log(f"      ➕ {operation_desc}: {rel_path} ({file_size} 字节)")
                    files_to_copy += 1
                    
                    op = FileOperation(
                        'copy', src_file, des_file, file_size,
                        is_overwrite=is_overwrite, operation_location='source', rule_index=rule_index
                    )
                    self.operations.append(op)
                else:
                    #self.log(f"      ⏭️ 跳过: {rel_path} (无需更新)")
                    pass
        
        self.log(f"  源文件夹扫描完成: {files_processed} 个文件，过滤 {files_filtered} 个，需复制 {files_to_copy} 个")
        
        # 如果是完全同步模式，处理目标文件夹中多余的文件
        if getattr(rule, 'change_mode', 'incremental') == 'sync':
            self.log(f"  完全同步模式：检查目标文件夹中多余的文件")
            self._process_sync_deletions(rule, src_dir, des_dir, rule_index)
        else:
            self.log(f"  增量更新模式：跳过删除检查")
    
    def _process_sync_deletions(self, rule: PathRule, src_dir: str, des_dir: str, rule_index: int):
        """
        处理完全同步模式下的删除操作
        
        Args:
            rule: 路径规则
            src_dir: 源文件夹路径
            des_dir: 目标文件夹路径
            rule_index: 规则索引
        """
        if not os.path.exists(des_dir):
            self.log(f"    目标文件夹不存在，无需删除: {des_dir}")
            return
        
        files_to_delete = 0
        files_filtered = 0
        
        for root, dirs, files in os.walk(des_dir):
            rel_root = os.path.relpath(root, des_dir)
            if rel_root == '.':
                rel_root = ''
            
            for file in files:
                des_file = os.path.join(root, file)
                rel_path = os.path.relpath(des_file, des_dir)
                src_file = os.path.join(src_dir, rel_path)
                
                # 检查源文件是否存在
                if not os.path.exists(src_file):
                    # 检查过滤条件（需要同时检查相对路径和对应的源路径）
                    # 使用相对路径过滤（相对于源目录）
                    rel_path_filtered = self._is_filtered_out(rel_path, rule.excludes, rule.includes)
                    # 使用绝对路径过滤（检查源路径是否被排除）
                    src_path_filtered = self._is_filtered_out("", rule.excludes, rule.includes, src_file)
                    
                    if rel_path_filtered or src_path_filtered:
                        files_filtered += 1
                        if files_filtered <= 3:  # 只显示前3个被过滤的删除操作
                            self.log(f"      🚫 跳过删除被过滤的文件: {rel_path}")
                        continue
                    
                    file_size = os.path.getsize(des_file)
                    self.log(f"      🗑️ 删除多余文件: {rel_path} ({file_size} 字节)")
                    files_to_delete += 1
                    
                    op = FileOperation(
                        'delete', '', des_file, file_size,
                        operation_location='target', rule_index=rule_index
                    )
                    self.operations.append(op)
        
        self.log(f"  同步删除检查完成: 需删除 {files_to_delete} 个文件，跳过 {files_filtered} 个被过滤的文件")
    
    def _should_copy_file(self, src_file: str, des_file: str, duplicate_mode: str) -> bool:
        """
        判断文件是否需要复制
        
        Args:
            src_file: 源文件路径
            des_file: 目标文件路径
            duplicate_mode: 重复文件处理模式
            
        Returns:
            是否需要复制
        """
        if os.path.exists(des_file):
            if duplicate_mode == 'skip':
                return False
            elif duplicate_mode == 'overwrite':
                return True
            elif duplicate_mode == 'check':
                try:
                    src_mtime = int(os.path.getmtime(src_file))
                    des_mtime = int(os.path.getmtime(des_file))
                    src_size = os.path.getsize(src_file)
                    des_size = os.path.getsize(des_file)
                    #return not (src_mtime == des_mtime and src_size == des_size)
                    #"""
                    if src_mtime == des_mtime and src_size == des_size:
                        return False
                    else:
                        # 详细比较差异
                        result = False
                        time_diff = abs(src_mtime - des_mtime)
                        size_diff = abs(src_size - des_size)
                        reason_parts = []
                        if time_diff > 10:
                            #reason_parts.append(f"时间差: {time_diff:.1f}秒")
                            result = True
                        if size_diff != 0:
                            #reason_parts.append(f"大小差: {size_diff}字节")
                            result = True
                        #reason = ", ".join(reason_parts)
                        #self.log(f"        需更新 ({reason})")
                        return result
                    #"""
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
        """
        为删除操作找到对应的源基础路径
        
        Args:
            des_path: 目标文件路径
            
        Returns:
            对应的源基础路径
        """
        # 通过查找哪个路径对包含这个目标路径来推断源路径
        import os
        
        # 这里简化处理：通过分析目标路径推断源路径
        # 在实际实现中，应该基于 PathRule 来匹配
        for root, dirs, files in os.walk(os.path.dirname(des_path)):
            # 简单启发式：寻找同名的可能源路径
            if os.path.basename(des_path) in files:
                # 找到了可能的对应源，返回其目录
                return root
        
        # 如果找不到，返回目标路径的父目录
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
