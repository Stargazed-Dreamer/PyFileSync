import os
from datetime import datetime
from typing import List, Tuple

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTextEdit, QGroupBox, QRadioButton, QCheckBox, QFileDialog, QMessageBox,
    QProgressBar, QSplitter, QScrollArea, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSizePolicy, QTabWidget, QApplication, QDialog
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from .models import FileOperation, PathRule
from .worker import BackupWorker
from .widgets import DropLineEdit
from .config import load_config, write_config, update_timestamp_file
from .progress import write_progress, has_progress_file, read_progress
from .preview import PreviewManager, PreviewWorker
from . import adb_bridge
from .adb_browser import PhoneBrowserDialog
from . import logger

b_progress_file_checked = False

class FileBackupTool(QMainWindow):
    """文件备份工具主窗口"""

    def __init__(self):
        super().__init__()
        self.config_file: str | None = None
        self.timestamp: datetime = datetime.now()
        self.path_rules: List[PathRule] = []
        self.operations: List[FileOperation] = []
        self.worker = None
        self.mode = 'backup'
        self.duplicate_mode = 'overwrite'
        self.edit_index: int | None = None
        
        # 初始化预览管理器
        self.preview_manager = PreviewManager(log_callback=self.log_message)
        self._preview_worker: PreviewWorker | None = None

        # ADB 可用性检测（项目迁移到无 ADB 环境时优雅降级）
        self._adb_available = adb_bridge.is_adb_available()

        self.init_ui()

        if not self._adb_available:
            self.src_browse_phone_btn.setEnabled(False)
            self.des_browse_phone_btn.setEnabled(False)
            self.src_browse_phone_btn.setToolTip("未检测到 ADB，请安装 Android Platform Tools")
            self.des_browse_phone_btn.setToolTip("未检测到 ADB，请安装 Android Platform Tools")
            self.log_message("⚠️ 未检测到 ADB，手机相关功能已禁用（本地备份不受影响）")
            logger.log("ADB 不可用，手机功能已禁用", 'WARNING')

    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle("文件备份工具 v1.1")
        self.setGeometry(100, 100, 1200, 800)
        self.setMinimumSize(800, 600)  # 设置最小窗口尺寸

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)

        # 创建主要内容区域
        main_content = QWidget()
        main_content_layout = QVBoxLayout(main_content)
        main_content_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(main_content)

        # 创建水平分割器
        main_splitter = QSplitter(Qt.Horizontal)
        main_content_layout.addWidget(main_splitter)

        # 左侧区域使用标签页
        self.tab_widget = QTabWidget()
        main_splitter.addWidget(self.tab_widget)

        # 创建标签页
        self.create_path_management_tab()
        self.create_operations_tab()

        # 右侧日志区域
        right_widget = self.create_log_section(self)
        right_widget.setMinimumWidth(200)
        right_widget.setMaximumWidth(800)
        main_splitter.addWidget(right_widget)

        main_splitter.setSizes([700, 500])  # 给左侧更多空间
        main_splitter.setChildrenCollapsible(True)
        main_splitter.setStretchFactor(0, 1)  # 左侧可拉伸
        main_splitter.setStretchFactor(1, 0)  # 右侧固定

        self.create_progress_section(main_layout)

        self.statusBar().showMessage("就绪")

    def create_path_management_tab(self):
        """创建路径管理标签页"""
        path_tab = QWidget()
        path_layout = QVBoxLayout(path_tab)
        
        # 创建垂直分割器用于可调整高度
        path_splitter = QSplitter(Qt.Vertical)
        path_splitter.setChildrenCollapsible(True)
        
        # 路径对管理组
        table_group = QGroupBox("路径对管理")
        table_layout = QVBoxLayout(table_group)

        self.path_table = QTableWidget()
        self.path_table.setColumnCount(7)
        self.path_table.setHorizontalHeaderLabels(["启用", "源路径", "目标路径", "相同策略", "增删策略", "移动策略", "操作"])

        header = self.path_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 启用列
        header.setSectionResizeMode(1, QHeaderView.Stretch)  # 源路径列
        header.setSectionResizeMode(2, QHeaderView.Stretch)  # 目标路径列
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 相同策略列
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # 增删策略列
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)  # 移动策略列
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)

        self.path_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.path_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.path_table.setMinimumHeight(200)  # 设置最小高度而不是最大高度
        self.path_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)  # 允许表格占据更多空间
        
        # 连接双击事件
        self.path_table.cellDoubleClicked.connect(self._on_path_double_clicked)
        
        # 连接列宽改变事件，用于动态更新路径显示
        header.sectionResized.connect(self._on_column_resized)

        table_layout.addWidget(self.path_table)

        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)
        button_layout.addStretch()
        self.save_btn = QPushButton("保存到文件")
        self.save_btn.clicked.connect(self.save_config_changes)
        # 严格控制按钮尺寸，防止在空间充足时变大
        self.save_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.save_btn.setMinimumWidth(100)
        self.save_btn.setMaximumWidth(100)
        button_layout.addWidget(self.save_btn)
        button_container.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        table_layout.addWidget(button_container)
        
        table_widget = QWidget()
        table_widget.setLayout(table_layout)
        path_splitter.addWidget(table_widget)
        
        # 路径编辑组
        edit_group = QGroupBox("路径编辑")
        edit_layout = QVBoxLayout(edit_group)
        
        # 源路径和目标路径放在一行
        paths_row = QHBoxLayout()
        
        src_layout = QVBoxLayout()
        src_layout.addWidget(QLabel("源路径:"))
        src_row = QHBoxLayout()
        self.src_input = DropLineEdit()
        self.src_input.setPlaceholderText("拖放文件或文件夹到这里")
        src_row.addWidget(self.src_input, 1)
        self.src_browse_phone_btn = QPushButton("浏览手机")
        self.src_browse_phone_btn.clicked.connect(lambda: self._browse_phone(self.src_input))
        src_row.addWidget(self.src_browse_phone_btn)
        src_layout.addLayout(src_row)
        paths_row.addLayout(src_layout, 1)
        
        des_layout = QVBoxLayout()
        des_layout.addWidget(QLabel("目标路径:"))
        des_row = QHBoxLayout()
        self.des_input = DropLineEdit()
        self.des_input.setPlaceholderText("拖放文件或文件夹到这里")
        des_row.addWidget(self.des_input, 1)
        self.des_browse_phone_btn = QPushButton("浏览手机")
        self.des_browse_phone_btn.clicked.connect(lambda: self._browse_phone(self.des_input))
        des_row.addWidget(self.des_browse_phone_btn)
        des_layout.addLayout(des_row)
        paths_row.addLayout(des_layout, 1)
        
        edit_layout.addLayout(paths_row)
        
        # 策略选择放在一行
        strategies_row = QHBoxLayout()
        
        # 增删文件处理策略
        change_group = QGroupBox("增删文件处理策略")
        change_layout = QVBoxLayout(change_group)
        self.rule_change_incremental = QRadioButton("增量更新")
        self.rule_change_incremental.setChecked(True)
        change_layout.addWidget(self.rule_change_incremental)
        self.rule_incremental_move = QCheckBox("识别并移动发生移动的文件")
        self.rule_incremental_move.setToolTip(
            "仅增量更新下可用：识别源侧被移动的文件，并把目标侧对应文件直接移到新位置"
        )
        self.rule_incremental_move.setStyleSheet("margin-left: 18px;")
        change_layout.addWidget(self.rule_incremental_move)
        self.rule_change_sync = QRadioButton("完全同步")
        change_layout.addWidget(self.rule_change_sync)
        # 复选框仅在选中“增量更新”时可用
        self.rule_change_incremental.toggled.connect(self.rule_incremental_move.setEnabled)
        strategies_row.addWidget(change_group, 1)
        
        # 相同文件处理策略
        rule_dup_group = QGroupBox("相同文件处理策略")
        rule_dup_layout = QVBoxLayout(rule_dup_group)
        self.rule_overwrite = QRadioButton("覆盖")
        self.rule_overwrite.setChecked(True)
        rule_dup_layout.addWidget(self.rule_overwrite)
        self.rule_skip = QRadioButton("跳过")
        rule_dup_layout.addWidget(self.rule_skip)
        self.rule_check = QRadioButton("检查日期和大小（相同则跳过）")
        rule_dup_layout.addWidget(self.rule_check)
        strategies_row.addWidget(rule_dup_group, 1)
        
        # 文件移动识别策略
        move_group = QGroupBox("文件移动识别策略")
        move_group.setToolTip(
            "识别源侧被移动的文件，目标侧直接移动对应文件，避免删除后重新传输"
        )
        move_layout = QVBoxLayout(move_group)
        self.rule_move_none = QRadioButton("不识别")
        self.rule_move_none.setChecked(True)
        move_layout.addWidget(self.rule_move_none)
        self.rule_move_meta = QRadioButton("按元信息识别（文件名+大小+修改时间）")
        move_layout.addWidget(self.rule_move_meta)
        self.rule_move_hash = QRadioButton("按哈希识别（MD5，可识别改名）")
        move_layout.addWidget(self.rule_move_hash)
        strategies_row.addWidget(move_group, 1)
        
        edit_layout.addLayout(strategies_row)
        
        # 过滤策略
        filter_group = QGroupBox("过滤策略")
        filter_layout = QVBoxLayout(filter_group)
        
        # 添加说明标签
        filter_info = QLabel("💡 支持相对路径（相对于源目录）和绝对路径（如 G:/folder）")
        filter_info.setStyleSheet("color: #666; font-size: 11px; margin-bottom: 5px;")
        filter_layout.addWidget(filter_info)
        
        fl_row1 = QHBoxLayout()
        fl_row1.addWidget(QLabel("排除："))
        self.exclude_input = DropLineEdit()
        self.exclude_input.setPlaceholderText("例如 ignored_dir（相对路径）或 G:/folder（绝对路径）")
        fl_row1.addWidget(self.exclude_input)
        self.add_exclude_btn = QPushButton("添加排除")
        self.add_exclude_btn.clicked.connect(self.add_exclude_item)
        fl_row1.addWidget(self.add_exclude_btn)
        filter_layout.addLayout(fl_row1)

        fl_row2 = QHBoxLayout()
        fl_row2.addWidget(QLabel("包含："))
        self.include_input = DropLineEdit()
        self.include_input.setPlaceholderText("作为排除的例外，例如 ignored_dir/special.exe（支持相对/绝对路径）")
        fl_row2.addWidget(self.include_input)
        self.add_include_btn = QPushButton("添加包含")
        self.add_include_btn.clicked.connect(self.add_include_item)
        fl_row2.addWidget(self.add_include_btn)
        filter_layout.addLayout(fl_row2)

        # 使用滚动区域来显示过滤规则，支持删除按钮
        filter_scroll = QScrollArea()
        filter_scroll.setWidgetResizable(True)
        filter_scroll.setMinimumHeight(80)
        filter_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # 创建容器widget来存放过滤规则
        self.filter_container = QWidget()
        self.filter_container_layout = QVBoxLayout(self.filter_container)
        self.filter_container_layout.setContentsMargins(2, 2, 2, 2)
        self.filter_container_layout.setSpacing(2)
        self.filter_container_layout.addStretch()  # 添加拉伸，让规则靠上显示
        
        filter_scroll.setWidget(self.filter_container)
        filter_layout.addWidget(filter_scroll)

        edit_layout.addWidget(filter_group)

        # 保存按钮
        save_btn_layout = QHBoxLayout()
        save_btn_layout.addStretch()
        self.add_btn = QPushButton("保存到内存")
        self.add_btn.clicked.connect(self.add_path_pair)
        # 严格控制按钮尺寸，防止在空间充足时变大
        self.add_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.add_btn.setMinimumWidth(100)
        self.add_btn.setMaximumWidth(100)
        save_btn_layout.addWidget(self.add_btn)
        edit_layout.addLayout(save_btn_layout)
        
        edit_widget = QWidget()
        edit_widget.setLayout(edit_layout)
        path_splitter.addWidget(edit_widget)
        
        # 设置初始大小比例
        path_splitter.setSizes([250, 450])  # 路径对管理250px, 路径编辑450px
        path_splitter.setStretchFactor(0, 1)  # 路径对管理可以拉伸
        path_splitter.setStretchFactor(1, 1)  # 路径编辑也可以拉伸
        
        path_layout.addWidget(path_splitter)
        
        # 添加到标签页
        self.tab_widget.addTab(path_tab, "路径管理")
    
    def create_operations_tab(self):
        """创建操作标签页"""
        ops_tab = QWidget()
        ops_layout = QVBoxLayout(ops_tab)
        
        # 添加特殊功能组
        special_group = QGroupBox("特殊功能")
        special_layout = QVBoxLayout()
        self.skip_older = QCheckBox("复制时，跳过源时间戳早于目标时间戳的文件对")
        special_layout.addWidget(self.skip_older)
        special_group.setLayout(special_layout)
        ops_layout.addWidget(special_group)
        
        # 操作清单组
        operations_group = QGroupBox("操作清单")
        operations_layout = QVBoxLayout()
        self.operations_display = QTextEdit()
        self.operations_display.setReadOnly(True)
        operations_layout.addWidget(self.operations_display)
        operations_group.setLayout(operations_layout)
        ops_layout.addWidget(operations_group)
        
        # 添加到标签页
        self.tab_widget.addTab(ops_tab, "操作预览")


    def _browse_phone(self, input_field):
        """打开手机目录浏览器，选中后填入对应输入框"""
        if not self._adb_available:
            QMessageBox.warning(self, "ADB 不可用",
                                "未检测到 ADB 命令。\n请安装 Android Platform Tools 并确保 adb 在 PATH 中。")
            return
        try:
            dialog = PhoneBrowserDialog(self)
            if dialog.exec() == QDialog.Accepted and dialog.selected_path:
                input_field.setText(dialog.selected_path)
                self.log_message(f"已选择手机路径: {dialog.selected_path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"浏览手机目录时出错:\n{str(e)}")

    def add_path_pair(self):
        """添加路径对到表格"""
        src_path = self.src_input.text().strip()
        des_path = self.des_input.text().strip()
        if not src_path or not des_path:
            QMessageBox.warning(self, "警告", "请输入源路径和目标路径")
            return
        
        # T15: 拒绝双 ADB 路径（不支持手机→手机）
        if adb_bridge.is_adb_path(src_path) and adb_bridge.is_adb_path(des_path):
            QMessageBox.warning(self, "警告", "不支持手机→手机复制（两端都是手机路径）。\n请将一端改为本地路径。")
            return
        
        # ADB 可用性校验：ADB 路径在无 ADB 环境下不可用
        if not self._adb_available and (adb_bridge.is_adb_path(src_path) or adb_bridge.is_adb_path(des_path)):
            QMessageBox.warning(self, "ADB 不可用",
                                "路径包含 adb:// 但未检测到 ADB 命令。\n请安装 Android Platform Tools。")
            return
        
        # T15: 路径有效性校验（本地 + ADB 统一）
        if not adb_bridge.exists(src_path):
            QMessageBox.warning(self, "警告", f"源路径不存在: {src_path}")
            return

        duplicate_mode = 'overwrite'
        if self.rule_skip.isChecked():
            duplicate_mode = 'skip'
        elif self.rule_check.isChecked():
            duplicate_mode = 'check'

        # 文件移动识别策略
        move_mode = 'none'
        if self.rule_move_meta.isChecked():
            move_mode = 'meta'
        elif self.rule_move_hash.isChecked():
            move_mode = 'hash'
        # 增量更新下的移动识别开关（完全同步时复选框不可用，不生效）
        move_in_incremental = (
            self.rule_incremental_move.isChecked()
            and self.rule_change_incremental.isChecked()
        )

        # 读取原始过滤规则
        excludes = self._read_filter_lines(prefix='❌')
        includes = self._read_filter_lines(prefix='✔')
        
        # 实现双边排除：对于每条排除规则，同时在源和目标侧都生效
        # 在 PathRule 中我们只需要存储一次，因为过滤逻辑会在两侧都应用
        final_excludes = excludes[:]  # 直接复制，不需要特殊处理
        final_includes = includes[:]  # 直接复制

        rule = PathRule(
            src_dir=src_path,
            des_dir=des_path,
            duplicate_mode=duplicate_mode,
            excludes=final_excludes,
            includes=final_includes,
            change_mode=('sync' if self.rule_change_sync.isChecked() else 'incremental'),
            move_mode=move_mode,
            move_in_incremental=move_in_incremental
        )
        if hasattr(self, 'edit_index') and self.edit_index is not None:
            self.path_rules[self.edit_index] = rule
            self.edit_index = None
            self.add_btn.setText("保存到内存")
        else:
            self.path_rules.append(rule)
        self.update_path_table()
        self.src_input.clear()
        self.des_input.clear()
        self.exclude_input.clear()
        self.include_input.clear()
        self._clear_filter_container()
        self.log_message(f"添加路径对: {src_path} -> {des_path}")

    def update_path_table(self):
        """更新路径对表格显示"""
        self.path_table.setRowCount(len(self.path_rules))
        for row, rule in enumerate(self.path_rules):
            # 添加启用/禁用复选框
            checkbox_widget = QWidget()
            checkbox_layout = QHBoxLayout(checkbox_widget)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            checkbox_layout.setAlignment(Qt.AlignCenter)
            
            enable_checkbox = QCheckBox()
            enable_checkbox.setChecked(rule.enabled)
            enable_checkbox.stateChanged.connect(lambda state, r=row, checkbox=enable_checkbox: self._on_enable_checkbox_changed(r, state))
            
            checkbox_layout.addWidget(enable_checkbox)
            self.path_table.setCellWidget(row, 0, checkbox_widget)
            
            # 使用优化后的路径显示，传递对应列索引
            src_item = self._create_path_item(rule.src_dir, 1)  # 源路径在第1列
            des_item = self._create_path_item(rule.des_dir, 2)  # 目标路径在第2列
            
            # 设置项，强制更新显示
            self.path_table.setItem(row, 1, src_item)
            self.path_table.setItem(row, 2, des_item)
            
            mode_cn = {
                'overwrite': '覆盖',
                'skip': '跳过',
                'check': '检查日期和大小'
            }.get(rule.duplicate_mode, '覆盖')
            self.path_table.setItem(row, 3, QTableWidgetItem(mode_cn))
            
            # 显示增删策略
            change_mode = getattr(rule, 'change_mode', 'incremental')
            change_mode_cn = {
                'incremental': '增量更新',
                'sync': '完全同步'
            }.get(change_mode, '增量更新')
            self.path_table.setItem(row, 4, QTableWidgetItem(change_mode_cn))
            
            # 显示移动策略（标注增量更新下是否实际生效）
            move_mode = getattr(rule, 'move_mode', 'none')
            move_method_cn = {'meta': '按元信息', 'hash': '按哈希'}.get(move_mode, '')
            if move_mode == 'none':
                move_mode_cn = '不识别'
            elif change_mode == 'sync' or getattr(rule, 'move_in_incremental', False):
                move_mode_cn = move_method_cn
                if change_mode == 'incremental':
                    move_mode_cn += ' (增量)'
            else:
                move_mode_cn = move_method_cn + ' (增量未启用)'
            self.path_table.setItem(row, 5, QTableWidgetItem(move_mode_cn))
            
            ops_widget = QWidget()
            ops_layout = QHBoxLayout(ops_widget)
            ops_layout.setContentsMargins(0, 0, 0, 0)
            edit_btn = QPushButton("编辑")
            edit_btn.clicked.connect(lambda checked, r=row: self.edit_path_rule(r))
            delete_btn = QPushButton("删除")
            delete_btn.clicked.connect(lambda checked, r=row: self.delete_path_pair(r))
            ops_layout.addWidget(edit_btn)
            ops_layout.addWidget(delete_btn)
            self.path_table.setCellWidget(row, 6, ops_widget)

    def delete_path_pair(self, row):
        """删除指定行的路径对"""
        if 0 <= row < len(self.path_rules):
            # 检查是否正在编辑被删除的路径
            if hasattr(self, 'edit_index') and self.edit_index is not None:
                if self.edit_index == row:
                    # 如果正在编辑被删除的路径，停止编辑状态
                    self.edit_index = None
                    self.add_btn.setText("保存到内存")
                    self.src_input.clear()
                    self.des_input.clear()
                    self.exclude_input.clear()
                    self.include_input.clear()
                    self._clear_filter_container()
                    self.log_message("已停止编辑被删除的路径")
                elif self.edit_index > row:
                    # 如果编辑的索引在被删除行之后，需要调整索引
                    self.edit_index -= 1
                    self.log_message(f"编辑索引已调整: {self.edit_index + 1} -> {self.edit_index}")
            
            rule = self.path_rules.pop(row)
            self.update_path_table()
            self.log_message(f"删除路径对: {rule.src_dir} -> {rule.des_dir}")

    def save_config_changes(self):
        """保存配置更改到文件"""
        # 如果没有配置文件，让用户选择保存位置
        if not self.config_file:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "选择配置文件保存位置", "", "文本文件 (*.txt);;所有文件 (*)"
            )
            if not file_path:
                return  # 用户取消了选择
            self.config_file = file_path
            self.config_label.setText(f"配置文件: {self.config_file}")
            self.reload_btn.setEnabled(True)
        
        if not self.path_rules:
            QMessageBox.warning(self, "警告", "没有路径对可保存")
            return
        try:
            write_config(self.config_file, self.timestamp, self.path_rules)
            self.log_message(f"配置文件保存成功: {len(self.path_rules)} 个路径对")
            QMessageBox.information(self, "成功", "配置文件保存成功")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存配置文件失败: {str(e)}")

    def create_log_section(self, parent_widget):
        """创建日志区域"""
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_label = QLabel("操作日志")
        log_label.setFont(QFont("", 10, QFont.Bold))
        log_layout.addWidget(log_label)
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        log_layout.addWidget(self.log_display)
        clear_log_btn = QPushButton("清除日志")
        clear_log_btn.clicked.connect(self.log_display.clear)
        log_layout.addWidget(clear_log_btn)
        return log_widget

    def create_progress_section(self, parent_layout):
        """创建进度区域"""
        progress_frame = QFrame()
        progress_frame.setFrameStyle(QFrame.Box)
        progress_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        progress_layout = QVBoxLayout(progress_frame)

        config_frame = QFrame()
        config_layout = QHBoxLayout(config_frame)

        self.config_label = QLabel("配置文件: 未选择")
        config_layout.addWidget(self.config_label)

        self.browse_btn = QPushButton("浏览配置文件")
        self.browse_btn.clicked.connect(self.browse_config_file)
        config_layout.addWidget(self.browse_btn)

        self.reload_btn = QPushButton("重新加载")
        self.reload_btn.clicked.connect(self.load_config)
        self.reload_btn.setEnabled(False)
        config_layout.addWidget(self.reload_btn)

        self.robocopy_check = QCheckBox("使用 RoboCopy（Windows）")
        self.robocopy_check.setChecked(True)
        self.robocopy_check.setEnabled(os.name == 'nt')
        self.robocopy_check.setToolTip("Windows 上按目录批量复制明确文件；不可用时自动回退")
        config_layout.addWidget(self.robocopy_check)

        config_layout.addStretch()

        self.preview_btn = QPushButton("预览操作")
        self.preview_btn.clicked.connect(self.preview_operations)
        self.preview_btn.setEnabled(False)
        config_layout.addWidget(self.preview_btn)

        self.execute_btn = QPushButton("执行备份")
        self.execute_btn.clicked.connect(self.execute_backup)
        self.execute_btn.setEnabled(False)
        config_layout.addWidget(self.execute_btn)

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.clicked.connect(self.pause_backup)
        self.pause_btn.setEnabled(False)
        config_layout.addWidget(self.pause_btn)
        
        self.resume_btn = QPushButton("继续")
        self.resume_btn.clicked.connect(self.resume_backup)
        self.resume_btn.setEnabled(False)
        self.resume_btn.hide()  # 默认隐藏
        config_layout.addWidget(self.resume_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_backup)
        self.stop_btn.setEnabled(False)
        config_layout.addWidget(self.stop_btn)

        bar_frame = QFrame()
        bar_layout = QHBoxLayout(bar_frame)
        bar_layout.addWidget(QLabel("进度:"))
        self.progress_bar = QProgressBar()
        bar_layout.addWidget(self.progress_bar)
        self.bar_label = QLabel("0/0")
        bar_layout.addWidget(self.bar_label)

        progress_layout.addWidget(config_frame)
        progress_layout.addWidget(bar_frame)
        parent_layout.addWidget(progress_frame)

    def browse_config_file(self):
        """浏览配置文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择配置文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if file_path:
            self.config_file = file_path
            self.config_label.setText(f"配置文件: {file_path}")
            self.reload_btn.setEnabled(True)
            self.load_config()

    def load_config(self):
        """加载配置文件"""
        if not self.config_file:
            return
        try:
            self.timestamp, self.path_rules = load_config(self.config_file)
            self.update_path_table()
            self.preview_btn.setEnabled(True)
            self.log_message(f"配置文件加载成功: {len(self.path_rules)} 个路径对")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载配置文件失败: {str(e)}")

    def preview_operations(self):
        """预览操作（后台线程执行，避免 UI 冻结）"""
        if not self.path_rules:
            self.log_message("没有路径规则可预览")
            return
        
        # 防止重复预览
        if self._preview_worker and self._preview_worker.isRunning():
            self.log_message("预览正在进行中，请稍候...")
            return

        # 如果没有配置文件，提醒用户路径表不会被保存
        if not self.config_file:
            reply = QMessageBox.question(
                self, "未读取配置文件", 
                "当前没有读取配置文件，预览操作时路径表不会被保存。\n\n是否继续预览？",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        # 只处理启用的路径规则
        enabled_rules = [rule for rule in self.path_rules if rule.enabled]
        if not enabled_rules:
            self.log_message("没有启用的路径规则可预览")
            return
        
        # ADB 可用性校验：ADB 路径在无 ADB 环境下不可用
        if not self._adb_available:
            has_adb_rule = any(
                adb_bridge.is_adb_path(rule.src_dir) or adb_bridge.is_adb_path(rule.des_dir)
                for rule in enabled_rules
            )
            if has_adb_rule:
                QMessageBox.warning(self, "ADB 不可用",
                                    "路径规则包含 ADB 路径，但未检测到 ADB 命令。\n请安装 Android Platform Tools。")
                return
        
        # 禁用预览按钮，防止重复点击
        self.preview_btn.setEnabled(False)
        self.preview_btn.setText("预览中...")
        self.log_message(f"开始预览 {len(enabled_rules)} 个路径规则（后台执行）")

        # 创建后台预览线程
        self._preview_worker = PreviewWorker(self.preview_manager, enabled_rules)
        self._preview_worker.logSignal.connect(self.log_message)
        self._preview_worker.finishedSignal.connect(self._on_preview_finished)
        self._preview_worker.start()

    def _on_preview_finished(self, operations: list):
        """预览完成回调（主线程执行）"""
        self.operations = operations
        self.preview_btn.setEnabled(True)
        self.preview_btn.setText("预览操作")

        if not operations:
            self.log_message("预览完成：无操作")
            return

        # 显示操作摘要
        summary = self.preview_manager.get_operation_summary()
        self.operations_display.clear()
        self.operations_display.append(summary)
        self.operations_display.append("-" * 50)

        # 使用合并算法显示详细操作列表
        merged_display = self.preview_manager.get_merged_operation_display()
        for line in merged_display:
            if line.strip():
                self.operations_display.append(line)

        # 预览完成后立即保存断点数据
        mode = "backup"
        self.mode = mode
        self.duplicate_mode = 'per_rule'
        write_progress(self.operations, 0, mode, self.duplicate_mode,
                       self.skip_older.isChecked(), self.timestamp, self.path_rules)
        self.log_message("已保存预览断点到 progress.txt")

        self.execute_btn.setEnabled(True)

        # 自动切换到操作预览标签页
        self.tab_widget.setCurrentIndex(1)

        self.log_message(f"预览完成: {len(self.operations)} 个操作待执行")

    def _selected_copy_backend(self) -> str:
        """返回当前选择的复制后端。"""
        return 'robocopy' if self.robocopy_check.isChecked() else 'python'

    def execute_backup(self):
        """执行备份"""
        if not self.operations:
            return

        # 使用默认的备份模式，具体同步策略由各路径规则决定
        mode = "backup"
        self.mode = mode

        self.duplicate_mode = 'per_rule'

        # T14: ADB 路径设备确认
        has_adb = any(
            adb_bridge.is_adb_path(op.src_path) or adb_bridge.is_adb_path(op.des_path)
            for op in self.operations if op.operation in ('copy', 'delete', 'move')
        )
        if has_adb:
            if not self._adb_available:
                QMessageBox.critical(self, "ADB 不可用",
                                     "操作包含 ADB 路径，但未检测到 ADB 命令。\n请安装 Android Platform Tools 并重试。")
                return
            try:
                devices = adb_bridge.list_devices()
            except OSError as e:
                QMessageBox.critical(self, "错误", f"无法获取设备列表:\n{e}")
                return

            connected = {d['serial']: d['model'] for d in devices}

            # 收集操作中涉及的所有设备序列号
            serials = set()
            for op in self.operations:
                for path in (op.src_path, op.des_path):
                    if adb_bridge.is_adb_path(path):
                        serial, _ = adb_bridge.parse_adb_path(path)
                        serials.add(serial)

            # 检查设备是否在线
            for serial in serials:
                if serial not in connected:
                    QMessageBox.warning(
                        self, "设备未连接",
                        f"设备 {serial} 未连接，无法执行备份。\n请确认手机已通过 USB 连接并启用了 USB 调试。"
                    )
                    return

            device_info = "\n".join(
                f"  {connected.get(s, s)} ({s})" for s in serials
            )
            reply = QMessageBox.question(
                self, "确认执行",
                f"确定要执行{mode}操作吗？\n共 {len(self.operations)} 个文件\n\n涉及设备:\n{device_info}",
                QMessageBox.Yes | QMessageBox.No
            )
        else:
            reply = QMessageBox.question(
                self, "确认执行",
                f"确定要执行{mode}操作吗？\n共 {len(self.operations)} 个文件",
                QMessageBox.Yes | QMessageBox.No
            )
        if reply != QMessageBox.Yes:
            return

        self.worker = BackupWorker(
            self.operations, mode, self.skip_older.isChecked(),
            self.timestamp, self.duplicate_mode, start_index=0,
            copy_backend=self._selected_copy_backend()
        )

        self.worker.progress_updated.connect(self.update_progress)
        self.worker.operation_updated.connect(self.update_operation_status)
        self.worker.log_message.connect(self.log_message)
        self.worker.finished.connect(self.backup_finished)

        # 设置正确的初始按钮状态
        self.execute_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.show()
        self.resume_btn.setEnabled(False)
        self.resume_btn.hide()
        self.preview_btn.setEnabled(False)
        self.progress_bar.setMaximum(len(self.operations))
        self.progress_bar.setValue(0)

        self.worker.start()
        self.log_message("开始执行备份操作...")

    def resume_backup(self):
        """继续备份（从上次暂停的位置开始）"""
        if not self.operations:
            return
            
        # 获取当前进度位置
        current_index = self.progress_bar.value()
        
        # 创建新的工作线程从当前索引开始
        self.worker = BackupWorker(
            self.operations, self.mode, self.skip_older.isChecked(),
            self.timestamp, self.duplicate_mode, start_index=current_index,
            copy_backend=self._selected_copy_backend()
        )
        
        self.worker.progress_updated.connect(self.update_progress)
        self.worker.operation_updated.connect(self.update_operation_status)
        self.worker.log_message.connect(self.log_message)
        self.worker.finished.connect(self.backup_finished)
        
        # 更新按钮状态
        self.execute_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.show()
        self.resume_btn.setEnabled(False)
        self.resume_btn.hide()
        self.preview_btn.setEnabled(False)
        
        self.worker.start()
        self.log_message(f"继续执行备份操作... 从第 {current_index + 1} 个文件开始")
    
    def stop_backup(self):
        """停止备份"""
        if self.worker:
            self.worker.stop()
            
        # 停止时重置按钮状态
        self.execute_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.show()
        self.resume_btn.setEnabled(False)
        self.resume_btn.hide()
        self.preview_btn.setEnabled(True)

    def pause_backup(self):
        """暂停备份（在当前文件完成后暂停，并写入进度）"""
        if self.worker:
            self.worker.pause()

    def update_progress(self, current: int, total: int):
        """更新进度"""
        self.progress_bar.setValue(current)
        self.bar_label.setText(f"{current}/{total}")

    def update_operation_status(self, path: str, status: str):
        """更新操作状态（预留：可在此同步操作清单状态）"""
        pass

    def backup_finished(self, success: bool):
        """备份完成"""
        # 检查是否是暂停状态
        is_paused = False
        if self.worker and hasattr(self.worker, 'pause_requested') and self.worker.pause_requested:
            is_paused = True
        
        if is_paused:
            # 暂停状态：显示继续按钮，隐藏暂停按钮
            self.execute_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.pause_btn.hide()
            self.resume_btn.setEnabled(True)
            self.resume_btn.show()
            self.preview_btn.setEnabled(False)
            
            # 保存当前进度
            idx = self.progress_bar.value()
            write_progress(self.operations, idx, self.mode, self.duplicate_mode, self.skip_older.isChecked(), self.timestamp, self.path_rules)
            self.log_message("已保存断点进度到 progress.txt")
            self.log_message("备份已暂停，点击【继续】按钮可继续执行")
        else:
            # 正常完成或停止状态
            self.execute_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.pause_btn.show()
            self.resume_btn.setEnabled(False)
            self.resume_btn.hide()
            self.preview_btn.setEnabled(True)
            
            if success and self.config_file:
                self.timestamp = datetime.now()
                update_timestamp_file(self.config_file, self.timestamp)
                self.log_message(f"备份完成，时间戳已更新: {self.timestamp.strftime('%Y.%m.%d %H:%M:%S')}")
            elif not success and not is_paused:
                # 非暂停导致的非成功完成（比如手动停止）
                idx = self.progress_bar.value()
                write_progress(self.operations, idx, self.mode, self.duplicate_mode, self.skip_older.isChecked(), self.timestamp, self.path_rules)
                self.log_message("已保存断点进度到 progress.txt")

    def log_message(self, message: str):
        """添加日志消息（同时显示到 UI 和写入文件）"""
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"[{ts}] {message}")
        logger.log(message)

    def add_exclude_item(self):
        text = self.exclude_input.text().strip()
        if text:
            self._add_filter_item(text, 'exclude')
            self.exclude_input.clear()

    def add_include_item(self):
        text = self.include_input.text().strip()
        if text:
            self._add_filter_item(text, 'include')
            self.include_input.clear()

    def _add_filter_item(self, text: str, item_type: str):
        """添加过滤项到容器中"""
        # 创建水平布局来容纳规则文本和删除按钮
        item_widget = QWidget()
        item_layout = QHBoxLayout(item_widget)
        item_layout.setContentsMargins(2, 2, 2, 2)
        
        # 创建标签显示规则文本
        prefix = "❌" if item_type == 'exclude' else "✔"
        label = QLabel(f"{prefix} {text}")
        label.setWordWrap(True)
        
        # 设置文字颜色
        if item_type == 'exclude':
            label.setStyleSheet("color: #d32f2f;")  # 红色
        else:
            label.setStyleSheet("color: #2e7d32;")  # 绿色
            
        item_layout.addWidget(label, 1)  # 标签占用剩余空间
        
        # 添加删除按钮
        delete_btn = QPushButton("×")
        delete_btn.setFixedSize(20, 20)
        delete_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: 2px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #d32f2f;
            }
            QPushButton:pressed {
                background-color: #b71c1c;
            }
        """)
        delete_btn.clicked.connect(lambda checked, widget=item_widget, t=item_type, content=text: 
                                 self._remove_filter_item(widget, t, content))
        item_layout.addWidget(delete_btn)
        
        # 将项目添加到容器中（在拉伸之前插入，让新项目显示在上面）
        self.filter_container_layout.insertWidget(self.filter_container_layout.count() - 1, item_widget)

    def _remove_filter_item(self, widget: QWidget, item_type: str, content: str):
        """移除指定的过滤项"""
        # 从布局中移除并删除小部件
        self.filter_container_layout.removeWidget(widget)
        widget.deleteLater()
        
        # 记录日志
        prefix = "排除" if item_type == 'exclude' else "包含"
        self.log_message(f"已移除{prefix}规则: {content}")

    def _clear_filter_container(self):
        """清除所有过滤规则显示"""
        # 移除除了最后一个拉伸之外的所有widget
        while self.filter_container_layout.count() > 1:
            item = self.filter_container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _read_filter_lines(self, prefix: str) -> List[str]:
        """从widget容器中读取过滤规则"""
        out: List[str] = []
        prefix_symbol = "❌" if prefix == '❌' else "✔"
        
        # 遍历所有子widget，除了最后一个拉伸
        for i in range(self.filter_container_layout.count() - 1):
            item = self.filter_container_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                # 查找widget中的QLabel
                for child in widget.findChildren(QLabel):
                    text = child.text()
                    if text.startswith(prefix_symbol + ' '):
                        content = text[len(prefix_symbol)+1:].strip()
                        out.append(content)
                        break
        return out

    def closeEvent(self, event):
        """窗口关闭事件：未完成时等待当前文件完成并保存进度"""
        if self.worker and self.stop_btn.isEnabled():
            self.worker.pause()
            self.log_message("正在暂停以保存进度...")
            # 简单等待：让线程完成当前项（不阻塞UI过久）
            self.worker.wait(5000)
            idx = self.progress_bar.value()
            write_progress(self.operations, idx, self.mode, self.duplicate_mode, self.skip_older.isChecked(), self.timestamp, self.path_rules)
            self.log_message("已保存断点进度到 progress.txt")
        event.accept()

    def showEvent(self, event):
        """
        窗口显示事件处理方法，在窗口首次显示时检查并提示加载进度文件。
    
        功能：
            - 调用父类的showEvent方法。
            - 在窗口首次显示时，检测是否存在进度文件，并提示用户是否加载。
            - 如果用户选择加载，则调用_load_and_display_progress方法。
            - 异常处理，记录错误信息。
    
        参数：
            event (QShowEvent): 窗口显示事件对象，包含事件相关信息。
    
        返回值：
            无返回值。
        """
        super().showEvent(event)
        # 启动时检测进度文件并提示读取
        try:
            # 使用全局变量，避免在程序生命周期内重复检查进度文件
            global b_progress_file_checked
            # 检查进度文件是否存在且尚未检查过
            if has_progress_file() and not b_progress_file_checked:
                # 弹出对话框询问用户是否加载进度
                reply = QMessageBox.question(self, "发现进度文件", "检测到 progress.txt，是否读取上次进度？",
                                             QMessageBox.Yes | QMessageBox.No)
                # 如果用户选择“是”，则加载并显示进度
                if reply == QMessageBox.Yes:
                    self._load_and_display_progress()
                # 标记进度文件已检查，避免再次提示
                b_progress_file_checked = True
        except Exception as e:
            # 记录加载进度文件时可能发生的异常
            self.log_message(f"加载进度文件时出错: {e}")
    
    def _load_and_display_progress(self):
        """读取并显示进度信息，不直接开始操作"""
        try:
            ops, idx, mode, skip_older, ts, path_rules, progress_info, duplicate_mode = read_progress()
            
            # 加载路径规则
            self.path_rules = path_rules
            self.update_path_table()
            
            # 设置操作和时间戳
            self.operations = ops
            self.timestamp = ts
            self.mode = mode
            self.duplicate_mode = duplicate_mode
            
            # 同步 skip_older 复选框状态
            self.skip_older.setChecked(skip_older)
            
            # 显示进度信息
            self.progress_bar.setMaximum(len(self.operations))
            self.progress_bar.setValue(idx)
            self.bar_label.setText(f"{idx}/{len(self.operations)}")
            
            # 设置按钮状态：处于暂停状态，可以继续
            self.execute_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.pause_btn.hide()
            self.resume_btn.setEnabled(True)
            self.resume_btn.show()
            self.preview_btn.setEnabled(True)
            
            # 切换到操作预览标签页
            self.tab_widget.setCurrentIndex(1)
            
            # 在操作清单中显示进度信息
            self.operations_display.clear()
            self.operations_display.append("=== 上次进度信息 ===")
            self.operations_display.append(f"进度时间: {progress_info.get('progress_time', '未知')}")
            self.operations_display.append(f"操作模式: {progress_info.get('mode', '未知')}")
            self.operations_display.append(f"跳过旧文件: {'是' if progress_info.get('skip_older') else '否'}")
            self.operations_display.append(f"时间戳: {progress_info.get('timestamp', '未知')}")
            self.operations_display.append(f"当前进度: {progress_info.get('index', 0)} / {progress_info.get('total_operations', 0)}")
            self.operations_display.append(f"路径规则数: {progress_info.get('path_rules_count', 0)}")
            self.operations_display.append("-" * 50)
            self.operations_display.append("点击【继续】按钮可继续执行，或【预览操作】重新生成预览")
            
            # 设置配置文件路径（如果有的话）
            if self.config_file:
                self.config_label.setText(f"配置文件: {self.config_file}")
                self.reload_btn.setEnabled(True)
            
            self.log_message(f"已读取上次进度: {len(ops)} 个操作，当前索引 {idx}")
            self.log_message("进度已加载，点击【继续】按钮可继续执行")
            
        except Exception as e:
            self.log_message(f"读取进度失败: {e}")
            QMessageBox.warning(self, "读取失败", f"读取进度文件时出错：{str(e)}")

    def edit_path_rule(self, row: int):
        """编辑指定路径规则：载入到路径编辑栏"""
        if 0 <= row < len(self.path_rules):
            rule = self.path_rules[row]
            self.src_input.setText(rule.src_dir)
            self.des_input.setText(rule.des_dir)
            self._clear_filter_container()
            for ex in rule.excludes:
                self._add_filter_item(ex, 'exclude')
            for inc in rule.includes:
                self._add_filter_item(inc, 'include')
            self.rule_overwrite.setChecked(rule.duplicate_mode == 'overwrite')
            self.rule_skip.setChecked(rule.duplicate_mode == 'skip')
            self.rule_check.setChecked(rule.duplicate_mode == 'check')
            cm = getattr(rule, 'change_mode', 'incremental')
            self.rule_change_incremental.setChecked(cm == 'incremental')
            self.rule_change_sync.setChecked(cm == 'sync')
            mm = getattr(rule, 'move_mode', 'none')
            self.rule_move_none.setChecked(mm == 'none')
            self.rule_move_meta.setChecked(mm == 'meta')
            self.rule_move_hash.setChecked(mm == 'hash')
            self.rule_incremental_move.setChecked(getattr(rule, 'move_in_incremental', False))
            self.edit_index = row
            self.add_btn.setText("保存")

    def _format_path_for_display(self, path: str, max_chars: int = 50) -> str:
        """优化路径显示：优先显示最后一段路径
        当路径很长时，显示为 ...最后一段 的形式
        
        Args:
            path: 要显示的路径
            max_chars: 最大显示字符数，默认为50
        """
        if len(path) <= max_chars:  # 如果路径不太长，直接显示
            return path
        
        # 确保最小显示长度
        if max_chars < 10:
            max_chars = 10
        
        # 获取最后一段路径
        last_part = os.path.basename(path)
        if not last_part:  # 如果最后一段为空（比如以/结尾的情况）
            # 获取倒数第二段
            parts = path.rstrip('/\\').split('/')
            if len(parts) > 1:
                last_part = parts[-2] if parts[-1] == '' else parts[-1]
            else:
                last_part = path
        
        # 如果最后一段本身就超过限制，直接截断
        if len(last_part) > max_chars - 3:  # 3是"..."的长度
            return "..." + last_part[-(max_chars-3):]
        
        # 计算可用于前缀的空间
        available_space = max_chars - len(last_part) - 3  # 3是"..."的长度
        if available_space > 0:
            # 尝试显示一些前缀
            prefix = path[:available_space]
            if prefix and not prefix.endswith(('/', '\\')):
                # 找到一个完整的分隔符
                last_sep = max(prefix.rfind('/'), prefix.rfind('\\'))
                if last_sep > 0:
                    prefix = prefix[:last_sep+1]
            return prefix + "..." + last_part
        else:
            return "..." + last_part

    def _show_full_path_dialog(self, path: str, title: str = "完整路径"):
        """显示完整路径的对话框"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
        
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(500)
        
        layout = QVBoxLayout(dialog)
        
        label = QLabel("完整路径:")
        layout.addWidget(label)
        
        path_label = QLabel(path)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(path_label)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        close_btn.setDefault(True)  # 默认选中关闭按钮
        button_layout.addWidget(close_btn)
        
        copy_btn = QPushButton("复制")
        copy_btn.clicked.connect(lambda: self._copy_to_clipboard(path))
        button_layout.addWidget(copy_btn)
        
        layout.addLayout(button_layout)
        
        dialog.exec()

    def _copy_to_clipboard(self, text: str):
        """复制文本到剪贴板"""
        from PySide6.QtGui import QClipboard
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        self.log_message("路径已复制到剪贴板")

    def _create_path_item(self, path: str, column: int = 1) -> QTableWidgetItem:
        """创建路径表格项，支持双击显示完整路径
        
        Args:
            path: 完整路径
            column: 列索引，用于确定显示宽度
        """
        item = QTableWidgetItem(self._format_path_for_display(path))
        item.setData(Qt.UserRole, path)  # 存储完整路径
        
        # 如果表格已经创建并且列宽可用，则使用动态宽度
        if hasattr(self, 'path_table') and self.path_table.columnCount() > 0:
            column_width = self.path_table.columnWidth(column)
            if column_width > 0:
                available_chars = self._calculate_chars_from_width(column_width)
                new_display = self._format_path_for_display(path, available_chars)
                item.setText(new_display)
        
        return item
        
    def _update_path_display_width(self):
        """更新路径显示，根据当前列宽调整"""
        # 遍历所有行，更新路径显示
        for row in range(self.path_table.rowCount()):
            src_item = self.path_table.item(row, 1)
            des_item = self.path_table.item(row, 2)
            
            if src_item:
                full_path = src_item.data(Qt.UserRole)
                if full_path:
                    # 获取当前列宽并计算可用字符数
                    column_width = self.path_table.columnWidth(1)
                    available_chars = self._calculate_chars_from_width(column_width)
                    new_display = self._format_path_for_display(full_path, available_chars)
                    src_item.setText(new_display)
            
            if des_item:
                full_path = des_item.data(Qt.UserRole)
                if full_path:
                    # 获取当前列宽并计算可用字符数
                    column_width = self.path_table.columnWidth(2)
                    available_chars = self._calculate_chars_from_width(column_width)
                    new_display = self._format_path_for_display(full_path, available_chars)
                    des_item.setText(new_display)

    def _on_path_double_clicked(self, row: int, column: int):
        """处理路径单元格的双击事件"""
        if column in [1, 2]:  # 源路径或目标路径列
            item = self.path_table.item(row, column)
            if item:
                full_path = item.data(Qt.UserRole)
                if full_path:
                     title = "源路径" if column == 1 else "目标路径"
                     self._show_full_path_dialog(full_path, title)

    def _on_column_resized(self, column: int, old_size: int, new_size: int):
        """处理列宽调整事件"""
        # 只对路径列（第1列和第2列）进行更新
        if column in [1, 2]:
            self._update_path_display_width_for_column(column)

    def _update_path_display_width_for_column(self, column: int):
        """更新指定列的路径显示宽度"""
        for row in range(self.path_table.rowCount()):
            item = self.path_table.item(row, column)
            if item:
                full_path = item.data(Qt.UserRole)
                if full_path:
                    column_width = self.path_table.columnWidth(column)
                    available_chars = self._calculate_chars_from_width(column_width)
                    new_display = self._format_path_for_display(full_path, available_chars)
                    item.setText(new_display)

    def _calculate_chars_from_width(self, width: int) -> int:
        """根据像素宽度和字体精确估算可用的字符数

        Args:
            width: 列宽（像素）

        Returns:
            可显示的字符数
        """
        # 1. 获取当前表格的字体
        font = self.path_table.font()

        # 2. 获取字体度量
        from PySide6.QtGui import QFontMetrics
        font_metrics = QFontMetrics(font)

        # 3. 考虑DPI缩放（Windows高DPI场景）
        # Windows 10/11的高DPI缩放会影响字体渲染
        # 使用默认96DPI作为基准
        dpi = 96
        scale_factor = dpi / 96.0

        # 4. 计算可用宽度（减去边距和padding）
        # 表格通常需要一些边距，比如1-2个字符的宽度
        padding = int(8 * scale_factor)  # 根据DPI调整padding

        available_width = width - padding

        if available_width <= 0:
            # 至少能显示2-3个字符
            return max(3, int(5 * scale_factor))

        # 5. 获取平均字符宽度（考虑不同字符类型）
        # 使用字体度量获取更准确的平均宽度
        avg_char_width = font_metrics.averageCharWidth()

        # 6. 考虑字符宽度的变异性
        # 某些字符（如i、l）较窄，某些（如W、M）较宽
        # 我们可以稍微保守一点，使用稍大的平均宽度
        avg_char_width_adjusted = avg_char_width * 1.1  # 增加10%的余量

        # 7. 计算字符数
        char_count = int(available_width / avg_char_width_adjusted)

        # 8. 设置合理范围
        # 最小字符数：确保至少能显示2-3个有意义的字符
        min_chars = max(3, int(5 * scale_factor))

        # 最大字符数：避免计算量过大，但允许更长的显示
        # 根据列宽动态调整最大值
        max_chars = min(300, int(width / 4))  # 最多显示300字符或宽度的1/4

        return max(min_chars, min(char_count, max_chars))

    def _on_enable_checkbox_changed(self, row: int, state: int):
        """处理启用/禁用复选框状态改变"""
        if 0 <= row < len(self.path_rules):
            # print(state)
            # 勾选为2 不勾选为0
            enabled = state == 2
            self.path_rules[row].enabled = enabled
            status = "启用" if enabled else "禁用"
            self.log_message(f"路径规则 {row + 1} 已{status}: {self.path_rules[row].src_dir} -> {self.path_rules[row].des_dir}")
