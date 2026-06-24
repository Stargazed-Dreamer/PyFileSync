from datetime import datetime
from typing import List, Dict, Any
import json

from models import FileOperation, PathRule

PROGRESS_FILE = 'progress.txt'

def write_progress(operations: List[FileOperation], current_index: int, mode: str,
                  duplicate_mode: str, skip_older: bool, timestamp: datetime, 
                  path_rules: List[PathRule] = None):
    """写入进度文件（人类可读 + JSON数据）
    新格式支持精细区分策略：
    
    [Header]
    *Progress YYYY.MM.DD HH:MM:SS
    Mode: backup
    SkipOlder: true|false
    Timestamp: YYYY.MM.DD HH:MM:SS
    Index: <当前索引>
    TotalOperations: <操作总数>
    
    [PathRules]
    # JSON格式的路径规则，支持路径级别的策略
    JSON_DATA
    
    [Operations]
    # 操作列表，与路径规则关联
    COPY|<rule_index> <src> -> <des>
    DELETE|<rule_index> <des>
    """
    
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        # 写入头部信息
        f.write(f"*Progress {datetime.now().strftime('%Y.%m.%d %H:%M:%S')}\n")
        f.write(f"[Header]\n")
        f.write(f"Mode: {mode}\n")
        f.write(f"SkipOlder: {'true' if skip_older else 'false'}\n")
        f.write(f"DuplicateMode: {duplicate_mode}\n")
        f.write(f"Timestamp: {timestamp.strftime('%Y.%m.%d %H:%M:%S')}\n")
        f.write(f"Index: {current_index}\n")
        f.write(f"TotalOperations: {len(operations)}\n")
        f.write(f"\n")
        
        # 写入路径规则（JSON格式）
        f.write(f"[PathRules]\n")
        if path_rules:
            path_rules_data = []
            for rule in path_rules:
                rule_dict = {
                    'src_dir': rule.src_dir,
                    'des_dir': rule.des_dir,
                    'duplicate_mode': rule.duplicate_mode,
                    'excludes': rule.excludes,
                    'includes': rule.includes,
                    'change_mode': getattr(rule, 'change_mode', 'incremental'),
                    'enabled': getattr(rule, 'enabled', True)
                }
                path_rules_data.append(rule_dict)
            f.write(json.dumps(path_rules_data, ensure_ascii=False, indent=2))
        f.write(f"\n\n")
        
        # 写入操作列表
        f.write(f"[Operations]\n")
        for i, op in enumerate(operations):
            # 计算操作对应的路径规则索引（简化处理，基于路径匹配）
            rule_index = _find_rule_index_for_operation(op, path_rules) if path_rules else 0
            
            if op.operation == 'copy':
                line = f"COPY|{rule_index} {op.src_path} -> {op.des_path}\n"
            elif op.operation == 'delete':
                line = f"DELETE|{rule_index} {op.des_path}\n"
            else:
                continue
            
            f.write(line)

def _find_rule_index_for_operation(operation: FileOperation, path_rules: List[PathRule]) -> int:
    """为操作找到对应的路径规则索引"""
    if not path_rules:
        return 0
    
    # 简化处理：根据路径匹配找到对应的规则
    if operation.operation == 'copy' and operation.src_path:
        for i, rule in enumerate(path_rules):
            if operation.src_path.startswith(rule.src_dir):
                return i
    
    if operation.des_path:
        for i, rule in enumerate(path_rules):
            if operation.des_path.startswith(rule.des_dir):
                return i
    
    return 0

def has_progress_file() -> bool:
    import os
    return os.path.exists(PROGRESS_FILE)

def read_progress() -> tuple[List[FileOperation], int, str, bool, datetime, List[PathRule], Dict[str, Any], str]:
    """读取进度文件
    返回：操作列表、当前索引、mode、skip_older、timestamp、path_rules、progress_info、duplicate_mode
    """
    operations: List[FileOperation] = []
    current_index = 0
    mode = 'backup'
    skip_older = False
    timestamp = datetime.now()
    path_rules: List[PathRule] = []
    progress_info: Dict[str, Any] = {}
    duplicate_mode = 'per_rule'

    with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    
    # 解析各个部分
    current_section = None
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 检查section标记
        if line.startswith('[') and line.endswith(']'):
            current_section = line[1:-1]
            i += 1
            continue
        
        # 跳过Progress时间戳行
        if line.startswith('*Progress'):
            progress_info['progress_time'] = line.split(' ', 1)[1] if ' ' in line else ''
            i += 1
            continue
        
        # 根据当前section解析内容
        if current_section == 'Header':
            if line.startswith('Mode:'):
                mode = line.split(':', 1)[1].strip()
                progress_info['mode'] = mode
            elif line.startswith('SkipOlder:'):
                skip_older = line.split(':', 1)[1].strip().lower() == 'true'
                progress_info['skip_older'] = skip_older
            elif line.startswith('DuplicateMode:'):
                duplicate_mode = line.split(':', 1)[1].strip()
                progress_info['duplicate_mode'] = duplicate_mode
            elif line.startswith('Timestamp:'):
                ts = line.split(':', 1)[1].strip()
                try:
                    timestamp = datetime.strptime(ts, '%Y.%m.%d %H:%M:%S')
                    progress_info['timestamp'] = ts
                except Exception:
                    pass
            elif line.startswith('Index:'):
                try:
                    current_index = int(line.split(':', 1)[1].strip())
                    progress_info['index'] = current_index
                except Exception:
                    current_index = 0
            elif line.startswith('TotalOperations:'):
                try:
                    total_ops = int(line.split(':', 1)[1].strip())
                    progress_info['total_operations'] = total_ops
                except Exception:
                    pass
                    
        elif current_section == 'PathRules':
            # 收集JSON数据
            json_lines = []
            while i < len(lines) and not lines[i].strip().startswith(']'):
                if lines[i].strip():
                    json_lines.append(lines[i])
                i += 1
            if json_lines:
                json_lines.append(lines[i])
                json_str = '\n'.join(json_lines)
                try:
                    path_rules_data = json.loads(json_str)
                    for rule_data in path_rules_data:
                        rule = PathRule(
                            src_dir=rule_data['src_dir'],
                            des_dir=rule_data['des_dir'],
                            duplicate_mode=rule_data['duplicate_mode'],
                            excludes=rule_data.get('excludes', []),
                            includes=rule_data.get('includes', []),
                            change_mode=rule_data.get('change_mode', 'incremental'),
                            enabled=rule_data.get('enabled', True)
                        )
                        path_rules.append(rule)
                    progress_info['path_rules_count'] = len(path_rules)
                except json.JSONDecodeError:
                    pass
            
        elif current_section == 'Operations':
            if line.startswith('COPY|'):
                # 格式：COPY|rule_index src -> des
                parts = line.split(' ', 1)
                if len(parts) >= 2:
                    rule_index = int(parts[0].split('|')[1])
                    src, des = parts[1].split(' -> ')
                    op = FileOperation('copy', src, des)
                    op.rule_index = rule_index
                    operations.append(op)
                        
            elif line.startswith('DELETE|'):
                # 格式：DELETE|rule_index des
                parts = line.split(' ', 1)
                if len(parts) >= 2:
                    rule_index = int(parts[0].split('|')[1])
                    des = parts[1]
                    op = FileOperation('delete', '', des)
                    op.rule_index = rule_index
                    operations.append(op)
        
        i += 1
    
    return operations, current_index, mode, skip_older, timestamp, path_rules, progress_info, duplicate_mode
