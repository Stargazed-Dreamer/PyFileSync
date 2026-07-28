"""手机目录浏览器

提供图形化目录树浏览 Android 设备文件系统。
支持多设备选择、懒加载（避免上千文件卡顿）、路径记忆。
"""

import json
import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QCheckBox, QLabel, QComboBox, QMessageBox
)
from PySide6.QtCore import Qt, QThread, Signal

from . import adb_bridge
from . import PROJECT_ROOT

STATE_FILE = os.path.join(PROJECT_ROOT, 'adb_browser_state.json')


class _ListDirWorker(QThread):
    """后台线程执行 ADB 目录列出，避免阻塞 UI"""
    dirListed = Signal(list)   # [(name, is_dir), ...]
    dirError = Signal(str)

    def __init__(self, adb_path: str):
        super().__init__()
        self.adb_path = adb_path

    def run(self):
        try:
            entries = adb_bridge.list_dir(self.adb_path)
            self.dirListed.emit(entries)
        except OSError as e:
            self.dirError.emit(str(e))


class PhoneBrowserDialog(QDialog):
    """手机目录浏览器对话框

    用法：
        dialog = PhoneBrowserDialog(self)
        if dialog.exec() == QDialog.Accepted:
            path = dialog.selected_path  # adb://serial/path
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("浏览手机目录")
        self.setMinimumSize(600, 500)

        self.selected_path: str | None = None
        self._devices: list[dict] = []
        self._current_serial: str = ''
        self._workers: list[_ListDirWorker] = []  # 防止 GC
        self._expand_chain: list[str] | None = None
        self._expand_root: QTreeWidgetItem | None = None

        self._init_ui()
        self._load_devices()

    # ── UI 构建 ──────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # 设备选择行
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("设备:"))
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        device_row.addWidget(self.device_combo, 1)
        self.device_label = QLabel("")
        device_row.addWidget(self.device_label)
        layout.addLayout(device_row)

        # 目录树
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("目录")
        self.tree.itemExpanded.connect(self._on_item_expanded)
        layout.addWidget(self.tree)

        # 底部按钮行
        bottom_row = QHBoxLayout()
        self.remember_check = QCheckBox("记住上次路径")
        self.remember_check.setChecked(True)
        bottom_row.addWidget(self.remember_check)
        bottom_row.addStretch()

        self.ok_btn = QPushButton("确定")
        self.ok_btn.clicked.connect(self._on_ok)
        self.ok_btn.setDefault(True)
        bottom_row.addWidget(self.ok_btn)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        bottom_row.addWidget(self.cancel_btn)

        layout.addLayout(bottom_row)

    # ── 设备管理 ────────────────────────────────────

    def _load_devices(self):
        """加载已连接的 ADB 设备列表"""
        try:
            self._devices = adb_bridge.list_devices()
        except OSError as e:
            QMessageBox.critical(self, "错误", f"无法获取设备列表:\n{e}")
            self.reject()
            return

        if not self._devices:
            QMessageBox.warning(
                self, "无设备",
                "未检测到已连接的 ADB 设备。\n"
                "请确认手机已通过 USB 连接并启用了 USB 调试。"
            )
            self.reject()
            return

        for dev in self._devices:
            label = f"{dev['model']} ({dev['serial']})"
            self.device_combo.addItem(label, dev['serial'])

        # 仅一台设备时隐藏选择器，只显示型号
        if len(self._devices) == 1:
            self.device_combo.setVisible(False)
            self.device_label.setText(f"{self._devices[0]['model']}")

    def _on_device_changed(self):
        """设备选择改变时，加载根目录"""
        idx = self.device_combo.currentIndex()
        if idx < 0 or idx >= len(self._devices):
            return

        self._current_serial = self._devices[idx]['serial']
        self.tree.clear()

        # 根节点 /sdcard
        root_path = adb_bridge.build_adb_path(self._current_serial, '/sdcard')
        root_item = QTreeWidgetItem(["/sdcard"])
        root_item.setData(0, Qt.UserRole, root_path)
        root_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        self.tree.addTopLevelItem(root_item)

        # 尝试恢复上次路径
        last_path = self._load_state(self._current_serial)
        if last_path:
            self._expand_to_path(root_item, last_path)
        else:
            root_item.setExpanded(True)

    # ── 懒加载 ──────────────────────────────────────

    def _on_item_expanded(self, item: QTreeWidgetItem):
        """节点展开时懒加载子目录"""
        adb_path = item.data(0, Qt.UserRole)
        if not adb_path:
            return

        # 已加载过则跳过
        if item.childCount() > 0:
            first_child = item.child(0)
            if first_child.data(0, Qt.UserRole) is not None:
                return

        # 清除占位符
        while item.childCount() > 0:
            item.takeChild(0)

        # 后台加载
        worker = _ListDirWorker(adb_path)
        worker.dirListed.connect(
            lambda entries, it=item: self._on_dir_loaded(it, entries)
        )
        worker.dirError.connect(
            lambda msg, it=item: self._on_dir_error(it, msg)
        )
        # QThread 内置 finished 信号（无参）用于清理引用
        worker.finished.connect(lambda w=worker: self._cleanup_worker(w))
        worker.start()
        self._workers.append(worker)

    def _on_dir_loaded(self, item: QTreeWidgetItem, entries: list):
        """目录加载完成，填充子节点并继续链式展开"""
        parent_path = item.data(0, Qt.UserRole)
        _, parent_android = adb_bridge.parse_adb_path(parent_path)

        for name, is_dir in entries:
            if name in ('.', '..'):
                continue
            child_android = parent_android.rstrip('/') + '/' + name
            full_path = adb_bridge.build_adb_path(self._current_serial, child_android)

            child_item = QTreeWidgetItem([name])
            child_item.setData(0, Qt.UserRole, full_path)
            if is_dir:
                child_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            item.addChild(child_item)

        # 链式展开：如果正在恢复上次路径，尝试展开下一级
        if self._expand_chain and self._expand_root is not None:
            target_name = self._expand_chain[0]
            for i in range(item.childCount()):
                child = item.child(i)
                if child.text(0) == target_name:
                    self._expand_chain.pop(0)
                    if self._expand_chain:
                        child.setExpanded(True)
                    else:
                        # 到达目标，选中它
                        self.tree.setCurrentItem(child)
                        self._expand_chain = None
                        self._expand_root = None
                    break
            else:
                # 未找到目标目录名，停止链式展开
                self._expand_chain = None
                self._expand_root = None

    def _on_dir_error(self, item: QTreeWidgetItem, msg: str):
        """目录加载失败"""
        error_item = QTreeWidgetItem([f"❌ 加载失败: {msg}"])
        error_item.setData(0, Qt.UserRole, None)
        item.addChild(error_item)

    def _cleanup_worker(self, worker: _ListDirWorker):
        """清理已完成的 worker 引用"""
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    # ── 路径记忆 ────────────────────────────────────

    def _expand_to_path(self, root_item: QTreeWidgetItem, target_path: str):
        """尝试逐级展开到上次路径

        由于懒加载是异步的，这里先展开根节点。
        后续级别的展开会在 _on_dir_loaded 中通过 _expand_chain 链式触发。
        """
        # 解析目标路径，提取 /sdcard 之后的各级目录
        _, android_path = adb_bridge.parse_adb_path(target_path)
        if android_path.startswith('/sdcard'):
            remaining = android_path[len('/sdcard'):]
        else:
            remaining = android_path

        self._expand_chain = [p for p in remaining.split('/') if p]
        self._expand_root = root_item
        root_item.setExpanded(True)

        # 如果没有后续层级，展开根节点即可
        if not self._expand_chain:
            self.tree.setCurrentItem(root_item)
            self._expand_chain = None
            self._expand_root = None

    # ── 确认/取消 ───────────────────────────────────

    def _on_ok(self):
        """确定按钮：返回选中路径"""
        current = self.tree.currentItem()
        if not current:
            QMessageBox.warning(self, "提示", "请选择一个目录")
            return

        path = current.data(0, Qt.UserRole)
        if not path:
            QMessageBox.warning(self, "提示", "请选择一个有效的目录")
            return

        self.selected_path = path

        if self.remember_check.isChecked():
            self._save_state(self._current_serial, path)

        self.accept()

    # ── 状态持久化 ──────────────────────────────────

    def _save_state(self, serial: str, path: str):
        """保存路径记忆到 JSON 文件（按设备序列号分别记忆）"""
        state = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        state[serial] = path
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_state(self, serial: str) -> str | None:
        """加载路径记忆"""
        if not os.path.exists(STATE_FILE):
            return None
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
            return state.get(serial)
        except (json.JSONDecodeError, OSError):
            return None
