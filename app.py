"""
Сборка в exe (Windows, из папки проекта):
    pip install pyinstaller
    pyinstaller --onefile --windowed --name MibParser app.py
"""

import os
import sys
import traceback
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor, QFont, QColor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QGroupBox,
    QFileDialog,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QFrame,
    QSplitter,
    QMessageBox,
    QHeaderView,
)
from jinja2.lexer import whitespace_re

import mib_core

# Фоновый поток, чтобы GUI не подвисал во время компиляции MIB
class PipelineThread(QThread):
    log_line = Signal(str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, input_dir, output_dir, parent=None):
        super().__init__(parent)
        self.input_dir = input_dir
        self.output_dir = output_dir

    def run(self):
        try:
            result = mib_core.run_pipeline(
                self.input_dir,
                self.output_dir,
                log_callback=self.log_line.emit,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")

# Небольшая карточка статистики
class StatCard(QFrame):
    def __init__(self, title, accent="#4A6CF7", parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setStyleSheet(
            f"""
            #statCard {{
                background: white;
                border: 1px solid #E3E6ED;
                border-radius: 10px;
            }}
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)

        self.value_label = QLabel("0")
        self.value_label.setStyleSheet(f"color:{accent}; font-size: 22px; font-weight: 700;")

        title_label = QLabel(title)
        title_label.setStyleSheet("color:#6B7280; font-size: 12px;")
        title_label.setWordWrap(True)

        layout.addWidget(self.value_label)
        layout.addWidget(title_label)

    def set_value(self, value):
        self.value_label.setText(str(value))

# Главное окно
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MIB Parser")
        self.resize(1200, 800)

        self.thread = None
        self.last_result = None

        self._build_ui()
        self._create_menu()
        self.statusBar().showMessage("Готово")
        self._apply_style()

    def _create_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("Файл")
        open_input = file_menu.addAction("Открыть папку MIB...")
        open_input.triggered.connect(self._browse_input_dir)

        open_output = file_menu.addAction("Открыть папку результата")
        open_output.triggered.connect(self._open_output_folder)

        file_menu.addSeparator()

        exit_action = file_menu.addAction("Выход")
        exit_action.triggered.connect(self.close)

        run_menu = menu.addMenu("Запуск")

        run_action = run_menu.addAction("Запустить парсинг")
        run_action.triggered.connect(self._start_pipeline)

        help_menu = menu.addMenu("Справка")

        about = help_menu.addAction("О программе")
        about.triggered.connect(
            lambda:
            QMessageBox.information(
                self,
                "MIB Parser",
                "Инструмент анализа и компиляции MIB файлов"
            )
        )
    def _build_ui(self):

        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(5, 5, 5, 5)

        splitter = QSplitter(Qt.Horizontal)

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())

        splitter.setSizes([260, 900])

        root.addWidget(splitter)

    def _build_left_panel(self):

        panel = QWidget()

        layout = QVBoxLayout(panel)

        layout.setSpacing(8)

        group = QGroupBox("Файлы")

        form = QVBoxLayout(group)

        form.addWidget(QLabel("Исходная папка MIB"))

        row1 = QHBoxLayout()

        self.input_dir_edit = QLineEdit()

        btn1 = QPushButton("...")

        btn1.clicked.connect(
            self._browse_input_dir
        )

        row1.addWidget(
            self.input_dir_edit
        )

        row1.addWidget(btn1)

        form.addLayout(row1)

        form.addWidget(
            QLabel("Папка результата")
        )

        row2 = QHBoxLayout()

        self.output_dir_edit = QLineEdit()

        btn2 = QPushButton("...")

        btn2.clicked.connect(
            self._browse_output_dir
        )

        row2.addWidget(
            self.output_dir_edit
        )

        row2.addWidget(btn2)

        form.addLayout(row2)

        layout.addWidget(group)

        layout.addStretch()

        self.btn_run = QPushButton(
            "Запустить"
        )

        self.btn_run.clicked.connect(
            self._start_pipeline
        )

        layout.addWidget(
            self.btn_run
        )

        self.btn_open_output = QPushButton(
            "Открыть результат"
        )

        self.btn_open_output.clicked.connect(
            self._open_output_folder
        )

        layout.addWidget(
            self.btn_open_output
        )

        return panel

    def _build_right_panel(self):

        panel = QWidget()

        layout = QVBoxLayout(panel)

        log_box = QGroupBox(
            "Журнал выполнения"
        )

        log_layout = QVBoxLayout(log_box)

        self.log_view = QPlainTextEdit()

        self.log_view.setReadOnly(True)

        font = QFont(
            "Consolas"
        )

        font.setPointSize(9)

        self.log_view.setFont(font)

        log_layout.addWidget(
            self.log_view
        )

        layout.addWidget(
            log_box,
            3
        )

        stats_box = QGroupBox(
            "Статистика"
        )

        stats_layout = QVBoxLayout(
            stats_box
        )

        self.stats_table = QTableWidget(
            5,
            2
        )

        self.stats_table.setHorizontalHeaderLabels(
            [
                "Параметр",
                "Значение"
            ]
        )

        rows = [
            "Найдено MIB",
            "Скомпилировано",
            "Ошибок",
            "Объектов MIB",
            "Notification-Type"
        ]

        for i, name in enumerate(rows):
            self.stats_table.setItem(
                i,
                0,
                QTableWidgetItem(name)
            )

            self.stats_table.setItem(
                i,
                1,
                QTableWidgetItem("0")
            )

        self.stats_table.horizontalHeader().setStretchLastSection(True)

        stats_layout.addWidget(
            self.stats_table
        )

        layout.addWidget(
            stats_box,
            1
        )

        return panel

    @staticmethod
    def _section_label(text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#2563EB; font-weight:600; font-size:13px;")
        return lbl

    def _apply_style(self):

        self.setStyleSheet(
            """ 
            QMenuBar {
                background-color: #D3D3D3;
                color: white;
            }
            QWidget {
                font-family: Segoe UI;
                font-size: 9pt;
                color: black;
            }
            QMainWindow {
                background:#F0F0F0;
            }
            QGroupBox {
                border: 1px solid #B8B8B8;
                margin-top: 8px;
                padding: 6px;
                font-weight: bold;
                background: #FAFAFA;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
            }
            QLineEdit {
                border: 1px solid #999;
                padding: 3px;
                background: white;
            }
            QPushButton {
                min-height: 24px;
                padding: 3px 10px;
                border: 1px solid #888;
                background: #EAEAEA;
            }
            QPushButton:hover {
                background: #DCDCDC;
            }
            QPlainTextEdit {
                background: #1E1E1E;
                color: #D4D4D4;
                border: 1px solid #555555;
                selection-background-color: #3A6EA5;
            }
            QTableWidget {
                background: white;
                gridline-color: #BFBFBF;
                border: 1px solid #999;
            }
            QHeaderView::section {
               background: #E0E0E0;
                color: black;
                border: 1px solid #999;
                padding: 3px;
                font-weight: bold;
            }
            QStatusBar {
                background: #E5E5E5;
            }
            """
        )

    # ----------------------------------------------------------- actions ---
    def _browse_input_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с MIB файлами")
        if path:
            self.input_dir_edit.setText(path)

    def _browse_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку для результатов")
        if path:
            self.output_dir_edit.setText(path)

    def _open_output_folder(self):
        path = self.output_dir_edit.text().strip()
        if not path or not os.path.isdir(path):
            QMessageBox.warning(self, "Папка не найдена", "Укажите существующую папку 'Сохранить'.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _start_pipeline(self):
        input_dir = self.input_dir_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()

        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "Ошибка", "Укажите корректную исходную папку с MIB файлами.")
            return
        if not output_dir:
            QMessageBox.warning(self, "Ошибка", "Укажите папку для сохранения результата.")
            return

        self.log_view.clear()
        self._reset_stats()

        self.btn_run.setEnabled(False)
        self.btn_run.setText("Парсинг выполняется...")

        self._append_log("Запуск парсинга...", level="STAGE")

        self.thread = PipelineThread(input_dir, output_dir)
        self.thread.log_line.connect(self._append_log)
        self.thread.finished_ok.connect(self._on_finished)
        self.thread.failed.connect(self._on_failed)
        self.thread.start()

    def _reset_stats(self):
        for i in range(5):
            self.stats_table.setItem(
                i,
                1,
                QTableWidgetItem("0")
            )

    # Уровни и их оформление. "STAGE" используется для заголовков этапов  пайплайна.
    _LOG_LEVEL_STYLES = {
        "ERROR": "#F87171",
        "WARN": "#FBBF24",
        "OK": "#4ADE80",
        "STAGE": "#60A5FA",
        "INFO": "#9CA3AF",
    }

    def _detect_level(self, text):
        lowered = text.lower()
        if "error" in lowered or "failing" in lowered or "ошибк" in lowered:
            return "ERROR"
        if "предупрежд" in lowered or "warn" in lowered:
            return "WARN"
        if "сохранён" in lowered or "compiled" in lowered or "готово" in lowered:
            return "OK"
        if lowered.rstrip().endswith("...") or "результаты" in lowered:
            return "STAGE"
        return "INFO"

    def _append_log(self, text, level=None):
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        timestamp = datetime.now().strftime("%H:%M:%S")
        level = level or self._detect_level(text)
        color = self._LOG_LEVEL_STYLES.get(level, "#D4D4D4")
        top_margin = "6px" if level == "STAGE" else "0px"

        html = (
            f'<div style="margin-top:{top_margin}; white-space:pre-wrap;">'
            f'<span style="color:#6B7280;">[{timestamp}]</span> '
            f'<span style="color:{color}; font-weight:bold;">[{level:<5}]</span> '
            f'<span style="color:#E5E7EB;">{self._escape_html(text)}</span>'
            f'</div>'
        )
        cursor.insertHtml(html)
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    @staticmethod
    def _escape_html(text):
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _on_finished(self, result):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("Запустить парсинг")
        self.last_result = result

        stats = result["stats"]
        values = [
            stats["found"],
            stats["compiled"],
            stats["errors"],
            stats["objects"],
            stats["notifications"]
        ]

        for i, value in enumerate(values):
            self.stats_table.setItem(
                i,
                1,
                QTableWidgetItem(str(value))
            )

        self._append_log(f"Готово. CSV: {result['output_csv']}", level="OK")
        if result.get("error_log"):
            self._append_log(f"Лог ошибок: {result['error_log']}", level="WARN")

        msg = f"Готово. CSV: {result['output_csv']}"
        if result.get("error_log"):
            msg += f"\nЛог ошибок: {result['error_log']}"
        QMessageBox.information(self, "Парсинг завершён", msg)

    def _on_failed(self, error_text):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("Запустить парсинг")
        self._append_log(error_text.split("\n\n")[0], level="ERROR")
        QMessageBox.critical(self, "Ошибка выполнения", error_text.split("\n\n")[0])


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()