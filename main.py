from PySide6.QtWidgets import QApplication
from ui import FileBackupTool

def main():
    app = QApplication([])
    app.setStyle('Fusion')
    window = FileBackupTool()
    window.show()
    app.exec()

if __name__ == "__main__":
    main()