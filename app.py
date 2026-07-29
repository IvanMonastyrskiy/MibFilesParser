"""
Сборка в exe (Windows, из папки проекта):
    pip install pyinstaller
    pyinstaller --onefile --windowed --name MibParser --collect-data pysmi app.py
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
    QComboBox,
    QCheckBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
)
from jinja2.lexer import whitespace_re

import mib_core

# Варианты кодового слова, по которому отбираются MIB-файлы для компиляции.
KEYWORD_OPTIONS = {
    "NOTIFICATION-TYPE": True,
    "TRAP-TYPE": False,
    "OBJECT-TYPE": False,
    "MODULE-IDENTITY": False,
}

# Поля атрибутов объекта, показываемые в панели снизу дерева MIB, и то, каким
# ключом они достаются из строки DataFrame (см. mib_core.parse_json_file).
DETAIL_FIELDS = [
    ("Name", "name"),
    ("OID", "oid"),
    ("Mib", "file_name"),
    ("Syntax", "syntax_display"),
    ("Access", "maxaccess"),
    ("Status", "status"),
    ("DefVal", "defval_display"),
    ("Indexes", "indices_display"),
]


# Фоновый поток, чтобы GUI не подвисал во время компиляции MIB
class PipelineThread(QThread):
    log_line = Signal(str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, input_dir, output_dir, keywords: list, parent=None):
        super().__init__(parent)
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.keywords = keywords          # ← список

    def run(self):
        try:
            result = mib_core.run_pipeline(
                self.input_dir,
                self.output_dir,
                log_callback=self.log_line.emit,
                keywords=self.keywords,
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
        self.resize(1300, 850)

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

        splitter.setSizes([260, 1000])

        root.addWidget(splitter)

    def _select_all_keywords(self):
        for cb in self.keyword_checkboxes.values():
            cb.setChecked(True)

    def _deselect_all_keywords(self):
        for cb in self.keyword_checkboxes.values():
            cb.setChecked(False)

    def _get_selected_keywords(self):
        """Возвращает список выбранных ключевых слов"""
        return [keyword for keyword, cb in self.keyword_checkboxes.items() if cb.isChecked()]

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

        parse_group = QGroupBox("Что парсить")
        parse_form = QVBoxLayout(parse_group)

        parse_form.addWidget(QLabel("Выберите типы объектов для обработки:"))

        self.keyword_checkboxes = {}

        for keyword, default_checked in KEYWORD_OPTIONS.items():
            cb = QCheckBox(keyword)
            cb.setChecked(default_checked)
            self.keyword_checkboxes[keyword] = cb
            parse_form.addWidget(cb)

        # Кнопки "Выбрать все / Снять все"
        btn_row = QHBoxLayout()
        btn_all = QPushButton("Выбрать все")
        btn_none = QPushButton("Снять все")

        btn_all.clicked.connect(self._select_all_keywords)
        btn_none.clicked.connect(self._deselect_all_keywords)

        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        parse_form.addLayout(btn_row)

        layout.addWidget(parse_group)
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
        """Правая часть окна — вкладки: 'Журнал и статистика' и 'Дерево MIB'."""

        tabs = QTabWidget()

        tabs.addTab(self._build_log_tab(), "Журнал и статистика")
        tabs.addTab(self._build_mib_tree_tab(), "Дерево MIB")

        return tabs

    def _build_log_tab(self):

        panel = QWidget()

        layout = QVBoxLayout(panel)

        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

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

    def _build_mib_tree_tab(self):
        """Вкладка 'Дерево MIB': иерархия объектов по OID сверху, атрибуты
        выбранного объекта (Name, OID, Mib, Syntax, Access, Status, DefVal,
        Indexes, Descr) — снизу, как в MIB Browser."""

        panel = QWidget()
        layout = QVBoxLayout(panel)

        splitter = QSplitter(Qt.Vertical)

        # --- Дерево ----------------------------------------------------
        tree_box = QGroupBox("Дерево MIB (иерархия по OID)")
        tree_layout = QVBoxLayout(tree_box)

        self.mib_tree = QTreeWidget()
        self.mib_tree.setHeaderLabels(["Объект"])
        self.mib_tree.setColumnCount(1)
        self.mib_tree.currentItemChanged.connect(self._on_tree_selection_changed)

        tree_layout.addWidget(self.mib_tree)
        splitter.addWidget(tree_box)

        # --- Атрибуты выбранного объекта --------------------------------
        detail_box = QGroupBox("Атрибуты выбранного объекта")
        detail_layout = QGridLayout(detail_box)
        detail_layout.setColumnStretch(1, 1)

        self.detail_fields = {}
        for row, (label_text, _key) in enumerate(DETAIL_FIELDS):
            detail_layout.addWidget(QLabel(label_text + ":"), row, 0)
            edit = QLineEdit()
            edit.setReadOnly(True)
            detail_layout.addWidget(edit, row, 1)
            self.detail_fields[label_text] = edit

        descr_row = len(DETAIL_FIELDS)
        detail_layout.addWidget(QLabel("Descr:"), descr_row, 0, Qt.AlignTop)
        self.descr_view = QPlainTextEdit()
        self.descr_view.setReadOnly(True)
        self.descr_view.setMaximumHeight(110)
        detail_layout.addWidget(self.descr_view, descr_row, 1)

        splitter.addWidget(detail_box)
        splitter.setSizes([550, 250])

        layout.addWidget(splitter)

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
                background: #F7F7F7;
            }
            QGroupBox {
                border: 1px solid #B8B8B8;
                border-radius: 6px;
                margin-top: 16px;
                padding-top: 10px;
                background: #FAFAFA;
                font-weight: bold;
            }

            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                background: #FAFAFA;
                color: black;
            }
            QLineEdit {
                border: 1px solid #999;
                padding: 3px;
                background: white;
            }
            QComboBox {
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
                background: #2B2B2B;
                color: #F0F0F0;
                border: 1px solid #B8B8B8;
                border-radius: 4px;
            }
            QTableWidget {
                background: white;
                gridline-color: #BFBFBF;
                border: 1px solid #999;
            }
            QTreeWidget {
                background: white;
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
        selected_keywords = self._get_selected_keywords()

        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "Ошибка", "Укажите корректную исходную папку с MIB файлами.")
            return
        if not output_dir:
            QMessageBox.warning(self, "Ошибка", "Укажите папку для сохранения результата.")
            return
        if not selected_keywords:
            QMessageBox.warning(self, "Ошибка", "Выберите хотя бы один тип для парсинга.")
            return

        self.log_view.clear()
        self._reset_stats()
        self._clear_mib_tree()

        self.btn_run.setEnabled(False)
        self.btn_run.setText("Парсинг выполняется...")

        keywords_str = ", ".join(selected_keywords)
        self._append_log(f"Запуск парсинга (типы: {keywords_str})...", level="STAGE")

        self.thread = PipelineThread(input_dir, output_dir, selected_keywords)
        self.thread.log_line.connect(self._append_log)
        self.thread.finished_ok.connect(self._on_finished)
        self.thread.failed.connect(self._on_failed)
        self.thread.start()

    def _reset_stats(self, keyword=None):
        if keyword:
            self.stats_table.setItem(4, 0, QTableWidgetItem(keyword))
        for i in range(5):
            self.stats_table.setItem(
                i,
                1,
                QTableWidgetItem("0")
            )

    def _clear_mib_tree(self):
        self.mib_tree.clear()
        self._clear_detail_fields()

    def _clear_detail_fields(self):
        for edit in self.detail_fields.values():
            edit.clear()
        self.descr_view.clear()

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

    # ------------------------------------------------------- дерево MIB ---
    def _populate_mib_tree(self, df):
        """Строит иерархическое дерево объектов по OID (как в MIB Browser).

        Родителем объекта считается объект из этого же набора данных с
        наибольшим по длине OID, являющимся строгим префиксом OID текущего
        объекта. Если такого нет — объект становится корневым узлом дерева.
        Объекты без валидного числового OID (например, определения типов
        TEXTUAL-CONVENTION) в дереве не показываются — у них просто нет OID.
        """
        self.mib_tree.clear()

        if df is None or len(df) == 0:
            return

        records = df.to_dict(orient="records")

        valid_records = []
        for rec in records:
            oid_str = rec.get("oid")
            if not oid_str or not isinstance(oid_str, str):
                continue
            try:
                oid_tuple = tuple(int(part) for part in oid_str.strip().split("."))
            except ValueError:
                continue
            if not oid_tuple:
                continue
            rec["_oid_tuple"] = oid_tuple
            valid_records.append(rec)

        if not valid_records:
            return

        # Родитель ищется быстро: по очереди отрезаем последний элемент OID и
        # проверяем, есть ли уже узел с таким OID в дереве. Сортировка по
        # возрастанию OID гарантирует, что потенциальный родитель уже создан
        # к моменту обработки потомка (префикс всегда "меньше" по кортежу).
        valid_records.sort(key=lambda r: r["_oid_tuple"])

        items_by_oid = {}
        for rec in valid_records:
            oid_tuple = rec["_oid_tuple"]

            parent_item = None
            for cut in range(len(oid_tuple) - 1, 0, -1):
                prefix = oid_tuple[:cut]
                if prefix in items_by_oid:
                    parent_item = items_by_oid[prefix]
                    break

            label = f"{rec.get('name') or '?'}  [{oid_tuple[-1]}]"
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.UserRole, rec)

            if parent_item is not None:
                parent_item.addChild(item)
            else:
                self.mib_tree.addTopLevelItem(item)

            # Если OID повторяется (редкий случай — объект встретился дважды),
            # оставляем в индексе первый узел, чтобы не путать родителей.
            items_by_oid.setdefault(oid_tuple, item)

        self.mib_tree.expandToDepth(1)

    def _on_tree_selection_changed(self, current, _previous):
        if current is None:
            self._clear_detail_fields()
            return

        rec = current.data(0, Qt.UserRole)
        if not rec:
            self._clear_detail_fields()
            return

        for label_text, key in DETAIL_FIELDS:
            value = rec.get(key)
            self.detail_fields[label_text].setText("" if value is None else str(value))

        descr = rec.get("description")
        self.descr_view.setPlainText("" if descr is None else str(descr))

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

        self._populate_mib_tree(result.get("df"))

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