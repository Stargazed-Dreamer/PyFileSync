from PySide6.QtWidgets import QApplication
from src.ui import FileBackupTool

def main():
    """
    功能：初始化并运行文件备份工具的GUI应用。
    参数：无。
    返回值：无。
    """
    app = QApplication([])  # 创建QApplication实例，参数为空列表，用于处理命令行参数
    app.setStyle('Fusion')  # 设置应用的视觉样式为'Fusion'
    window = FileBackupTool()  # 创建FileBackupTool窗口实例
    window.show()  # 显示窗口
    app.exec()  # 进入应用的主事件循环

if __name__ == "__main__":
    main()