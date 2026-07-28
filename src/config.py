from datetime import datetime
from typing import List
import os

from .models import PathRule

def load_config(file_path: str) -> tuple[datetime, List[PathRule]]:
    """加载配置文件（新格式）
    格式示例：
    *2025.11.16 10:00:00
    C:\\test -> E:\\test
    C:\\root → D:\\rootBackup |重复文件:检查日期和大小
      ❌ ignored_file.txt
      ❌ ignored_dir
        ✔ 仍然备份的文件.exe

    说明：
    - 顶行以 `*YYYY.MM.DD HH:MM:SS` 表示时间戳（用于“跳过早于时间戳”）
    - 路径对使用 `->` 或 `→` 分隔，允许尾部注释 `|重复文件:<覆盖|跳过|检查日期和大小>`
    - 过滤规则行以空格缩进，并以 `❌` 表示排除，`✔` 表示包含（作为排除例外）
    - 过滤规则为相对路径（相对于源目录）
    - `#`起始的行视为注释
    """
    timestamp = datetime.now()
    rules: List[PathRule] = []

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 时间戳
    if lines and lines[0].startswith('*'):
        ts = lines[0][1:].strip()
        try:
            timestamp = datetime.strptime(ts, "%Y.%m.%d %H:%M:%S")
        except Exception:
            timestamp = datetime.now()

    current: PathRule | None = None
    for raw in lines[1:]:
        line = raw.rstrip('\n')
        if not line.strip():
            continue
        if line.strip().startswith('#'):
            continue

        # 顶层行：路径对
        if line[0] != ' ' and line[0] != '\t':
            parts = line.split('|')
            pair = parts[0].strip()
            meta = '|'.join(parts[1:]).strip() if len(parts) > 1 else ''

            sep = '→' if '→' in pair else '->'
            if sep not in pair:
                continue
            src, des = [p.strip() for p in pair.split(sep, 1)]

            duplicate_mode = 'overwrite'
            if '重复文件' in meta:
                if '检查日期和大小' in meta:
                    duplicate_mode = 'check'
                elif '跳过' in meta:
                    duplicate_mode = 'skip'
                elif '覆盖' in meta:
                    duplicate_mode = 'overwrite'

            change_mode = 'incremental'
            if '增删文件处理' in meta:
                if '完全同步' in meta:
                    change_mode = 'sync'
                elif '增量更新' in meta:
                    change_mode = 'incremental'
            
            # 解析启用状态
            enabled = True
            if '状态:禁用' in meta:
                enabled = False
            elif '状态:启用' in meta:
                enabled = True

            current = PathRule(src_dir=src, des_dir=des, duplicate_mode=duplicate_mode, 
                             change_mode=change_mode, enabled=enabled)
            rules.append(current)
            continue

        # 缩进行：过滤规则
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith('❌ '):
            rel = stripped[2:].strip()
            current.excludes.append(rel)
        elif stripped.startswith('✔ '):
            rel = stripped[1:].strip()
            current.includes.append(rel)

    return timestamp, rules

def write_config(file_path: str, timestamp: datetime, rules: List[PathRule]):
    """写入配置文件（新格式）
    - 备份原文件到 `<file_path>_old`
    - 顶行时间戳，随后为每个路径规则与其过滤子行
    - 规则行：`SRC → DEST |重复文件:<覆盖|跳过|检查日期和大小>`
    - 过滤行：以两个空格缩进并以 `❌` 或 `✔` 前缀
    """
    import shutil

    backup_file = file_path + "_old"
    if os.path.exists(file_path):
        shutil.copy2(file_path, backup_file)

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(f"*{timestamp.strftime('%Y.%m.%d %H:%M:%S')}\n")
        for rule in rules:
            mode_cn = {
                'overwrite': '覆盖',
                'skip': '跳过',
                'check': '检查日期和大小'
            }.get(rule.duplicate_mode, '覆盖')
            change_cn = '完全同步' if getattr(rule, 'change_mode', 'incremental') == 'sync' else '增量更新'
            enabled_cn = '启用' if rule.enabled else '禁用'
            f.write(f"{rule.src_dir} → {rule.des_dir} |重复文件:{mode_cn} |增删文件处理:{change_cn} |状态:{enabled_cn}\n")
            for ex in rule.excludes:
                f.write(f"  ❌ {ex}\n")
            for inc in rule.includes:
                f.write(f"  ✔ {inc}\n")

def update_timestamp_file(file_path: str, timestamp: datetime):
    """更新时间戳到配置文件首行"""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    if lines:
        lines[0] = f"*{timestamp.strftime('%Y.%m.%d %H:%M:%S')}\n"
    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)