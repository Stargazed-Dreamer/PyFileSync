from PySide6.QtWidgets import QLineEdit
from PySide6.QtGui import QDropEvent, QDragEnterEvent

class DropLineEdit(QLineEdit):
    """支持拖放的文本输入框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        """拖拽进入事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        """拖拽放下事件"""
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile()
                self.setText(path)
                event.acceptProposedAction()
        else:
            event.ignore()