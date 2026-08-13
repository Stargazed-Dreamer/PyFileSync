from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class FileOperation:
    """文件操作数据类
    - operation: 'copy' 或 'delete'
    - src_path: 源文件路径
    - des_path: 目标文件路径
    - size: 文件大小（字节）
    - status: 当前状态：pending/success/failed/skipped
    - is_overwrite: 是否覆盖操作（True 表示覆盖，False 表示新复制）
    - operation_location: 操作位置：'source'（从源）或 'target'（目标）
    - rule_index: 对应的路径规则索引（用于进度恢复）
    """
    operation: str
    src_path: str
    des_path: str
    size: int = 0
    status: str = 'pending'
    is_overwrite: bool = False
    operation_location: str = 'source'
    rule_index: int = 0

@dataclass
class PathRule:
    """路径规则数据结构（新配置格式）
    - src_dir: 源目录
    - des_dir: 目标目录
    - duplicate_mode: 相同文件策略：overwrite/skip/check
    - change_mode: 增删文件处理：incremental/sync
    - move_mode: 文件移动识别：none/meta/hash
    - move_in_incremental: 增量更新下是否启用移动识别
    - excludes: 排除列表（相对路径）
    - includes: 包含列表（相对路径，作为排除的例外）
    - enabled: 是否启用此规则
    """
    src_dir: str
    des_dir: str
    duplicate_mode: str = 'overwrite'
    change_mode: str = 'incremental'  # 增删文件处理：incremental（增量更新）或 sync（完全同步）
    move_mode: str = 'none'  # 文件移动识别：none（不识别）/ meta（文件名+大小+修改时间）/ hash（MD5 哈希）
    move_in_incremental: bool = False  # 增量更新模式下是否启用移动识别（完全同步模式直接按 move_mode 生效）
    excludes: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    enabled: bool = True

@dataclass
class ResumeState:
    """断点续传状态占位数据结构（预留扩展）
    - last_success_time: 最近一次成功时间戳
    - checkpoint_path: 检查点文件路径（用于保存进度）
    - enabled: 是否启用断点续传（未来启用）
    注：当前不启用，仅为未来功能扩展预留接口。
    """
    last_success_time: datetime | None = None
    checkpoint_path: str | None = None
    enabled: bool = False