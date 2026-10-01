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
import ast
import pandas as pd

ALL_MODULES_LABEL = "Все модули"
import mib_core

# Варианты кодового слова, по которому отбираются MIB-файлы для компиляции.
KEYWORD_OPTIONS = {
    "NOTIFICATION-TYPE": True,
    "TRAP-TYPE": False,
    "OBJECT-TYPE": False,
    "MODULE-IDENTITY": False,
}

# Варианты типа объекта (JSON-поле "class" из pysmi) для фильтра дерева MIB.
# По умолчанию показываем всё — в отличие от KEYWORD_OPTIONS (которые
# определяют, какие ФАЙЛЫ вообще компилировать), здесь речь о том, какие
# уже скомпилированные объекты показать в дереве-браузере.
TREE_TYPE_OPTIONS = {
    "OBJECT-TYPE": True,
    "NOTIFICATION-TYPE": True,
    "MODULE-IDENTITY": True,
    "OBJECT-IDENTITY": True,
}

# Поля атрибутов объекта, показываемые в панели снизу дерева MIB (кроме
# Descr и Objects — те выводятся отдельно, ниже, в развёрнутом виде).
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


def _normalize_type_name(value):
    """Приводит 'OBJECT-TYPE' / 'objecttype' / None к единому виду для
    сравнения выбранных чекбоксов с JSON-полем 'class' из pysmi."""
    if not value:
        return ""
    return str(value).replace("-", "").replace("_", "").lower()

def _parse_objects_cell(value):
    """Столбец 'objects' из CSV -> список строк. Понимает и "['a', 'b']"
    (так пишет to_csv для списка), и 'a, b'."""
    if isinstance(value, list):
        return value
    text = str(value).strip() if value is not None else ""
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(v) for v in parsed]
        except (ValueError, SyntaxError):
            pass
    return [p.strip() for p in text.split(",") if p.strip()]

def _format_detail_value(value):
    """Приводит значение атрибута к отображаемой строке. Списки (например,
    'objects') соединяются через запятую вместо python-репрезентации."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    # pandas может отдать NaN как float; не показываем его как "nan"
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value)


# Фоновый поток, чтобы GUI не подвисал во время компиляции MIB
class PipelineThread(QThread):
    log_line = Signal(str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, input_dir, output_dir, keywords: list, detect_unsafe_issues: bool, parent=None):
        super().__init__(parent)
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.keywords = keywords
        self.detect_unsafe_issues = detect_unsafe_issues

    def run(self):
        try:
            result = mib_core.run_pipeline(
                self.input_dir,
                self.output_dir,
                log_callback=self.log_line.emit,
                keywords=self.keywords,
                detect_unsafe_issues=self.detect_unsafe_issues,
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
        self._last_df = None  # DataFrame последнего успешного прогона (для фильтров дерева)

        self._build_ui()
        self._create_menu()
        self.statusBar().showMessage("Готово")
        self._apply_style()

    def _create_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("Файл")
        open_input = file_menu.addAction("Открыть папку MIB...")
        open_input.triggered.connect(self._browse_input_dir)

        load_csv = file_menu.addAction("Загрузить output_mib.csv...")
        load_csv.triggered.connect(self._load_csv_file)

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
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        group = QGroupBox("Файлы")

        form = QVBoxLayout(group)

        form.addWidget(QLabel("Исходная папка MIB"))

        row1 = QHBoxLayout()

        self.input_dir_edit = QLineEdit()

        btn1 = QPushButton("...")
        btn1.setFixedWidth(36)
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
        btn2.setFixedWidth(36)
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

        # --- Дополнительные опции парсинга ----------------------------
        options_group = QGroupBox("Дополнительно")
        options_form = QVBoxLayout(options_group)

        self.check_detect_unsafe = QCheckBox("Проверять потенциальные проблемы")
        self.check_detect_unsafe.setChecked(True)
        self.check_detect_unsafe.setToolTip(
            "Диагностика непарных кавычек \" и фигурных скобок { } в MIB-файлах.\n"
            "Файлы при этом не изменяются — это только предупреждение в лог.\n"
            "Отключите, если такие предупреждения мешают (например, если они\n"
            "срабатывают на старом закомментированном коде)."
        )
        options_form.addWidget(self.check_detect_unsafe)

        layout.addWidget(options_group)

        layout.addStretch()

        self.btn_run = QPushButton(
            "Запустить"
        )

        self.btn_run.setObjectName("primaryButton")

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
        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(self._build_log_tab(), "Журнал и статистика")
        self.right_tabs.addTab(self._build_mib_tree_tab(), "Дерево MIB")
        return self.right_tabs

    def _build_log_tab(self):

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

    def _load_csv_file(self):
        start_dir = self.output_dir_edit.text().strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите output_mib.csv", start_dir, "CSV (*.csv);;Все файлы (*)"
        )
        if not path:
            return
        try:
            df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False,
                             encoding="utf-8-sig")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка чтения CSV", f"{path}\n\n{e}")
            return

        missing = {"name", "oid", "class", "file_name"} - set(df.columns)
        if missing:
            QMessageBox.warning(
                self, "Неподходящий CSV",
                "В файле нет обязательных столбцов: " + ", ".join(sorted(missing))
            )
            return

        if "objects" in df.columns:
            df["objects"] = df["objects"].apply(_parse_objects_cell)
        else:
            df["objects"] = [[] for _ in range(len(df))]

        self.right_tabs.setCurrentIndex(1)
        self._populate_mib_tree(df)
        self.statusBar().showMessage(f"Загружен CSV: {path} ({len(df)} объектов)")

    def _refresh_module_combo(self, df):
        """Заполняет выпадающий список уникальными значениями file_name.
        Первый пункт — «Все модули» (без фильтрации); выбор сбрасывается на него."""
        self.tree_module_combo.blockSignals(True)
        self.tree_module_combo.clear()
        self.tree_module_combo.addItem(ALL_MODULES_LABEL)
        if df is not None and "file_name" in df.columns:
            names = sorted(
                {str(n) for n in df["file_name"] if str(n).strip() and str(n) != "nan"},
                key=str.lower,
            )
            self.tree_module_combo.addItems(names)
        self.tree_module_combo.setCurrentIndex(0)
        self.tree_module_combo.blockSignals(False)

    def _get_selected_module(self):
        """None — показывать все модули."""
        if self.tree_module_combo.currentIndex() <= 0:
            return None
        return self.tree_module_combo.currentText()

    def _on_tree_module_filter_changed(self, _index):
        self._rebuild_mib_tree()
        self._apply_tree_search_filter(self.tree_search_edit.text())
    def _build_mib_tree_tab(self):
        """Вкладка 'Дерево MIB': иерархия объектов по OID сверху (с поиском
        и фильтром по типу объекта), атрибуты выбранного объекта — снизу,
        как в MIB Browser."""

        panel = QWidget()
        layout = QVBoxLayout(panel)

        splitter = QSplitter(Qt.Vertical)

        # --- Дерево + поиск + фильтр по типу ----------------------------
        tree_box = QGroupBox("Дерево MIB (иерархия по OID)")
        tree_layout = QVBoxLayout(tree_box)

        source_row = QHBoxLayout()
        self.btn_load_csv = QPushButton("Загрузить CSV...")
        self.btn_load_csv.clicked.connect(self._load_csv_file)
        source_row.addWidget(self.btn_load_csv)

        source_row.addWidget(QLabel("Модуль:"))
        self.tree_module_combo = QComboBox()
        self.tree_module_combo.addItem(ALL_MODULES_LABEL)
        self.tree_module_combo.setMinimumWidth(220)
        self.tree_module_combo.currentIndexChanged.connect(self._on_tree_module_filter_changed)
        source_row.addWidget(self.tree_module_combo, 1)
        tree_layout.addLayout(source_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Поиск:"))
        self.tree_search_edit = QLineEdit()
        self.tree_search_edit.setPlaceholderText("Имя объекта или OID (например, 1.3.6.1.4)...")
        self.tree_search_edit.textChanged.connect(self._apply_tree_search_filter)
        search_row.addWidget(self.tree_search_edit)
        tree_layout.addLayout(search_row)

        type_filter_box = QGroupBox("Показывать типы")
        type_filter_box.setObjectName("treeTypeFilter")
        type_filter_box.setMaximumHeight(55)
        type_filter_layout = QHBoxLayout(type_filter_box)
        type_filter_layout.setContentsMargins(8, 4, 8, 4)
        type_filter_layout.setSpacing(10)
        self.tree_type_checkboxes = {}
        for type_name, default_checked in TREE_TYPE_OPTIONS.items():
            cb = QCheckBox(type_name)
            cb.setChecked(default_checked)
            cb.stateChanged.connect(self._on_tree_type_filter_changed)
            self.tree_type_checkboxes[type_name] = cb
            type_filter_layout.addWidget(cb)
        tree_layout.addWidget(type_filter_box)

        self.mib_tree = QTreeWidget()
        self.mib_tree.setHeaderLabels(["Объект"])
        self.mib_tree.setColumnCount(1)
        self.mib_tree.currentItemChanged.connect(self._on_tree_selection_changed)

        tree_layout.addWidget(self.mib_tree)
        splitter.addWidget(tree_box)

        # --- Атрибуты выбранного объекта --------------------------------
        detail_box = QGroupBox("Атрибуты выбранного объекта")

        detail_layout = QGridLayout(detail_box)

        # Делаем внутренние отступы и расстояния между строками компактнее
        detail_layout.setContentsMargins(8, 8, 8, 8)
        detail_layout.setHorizontalSpacing(6)
        detail_layout.setVerticalSpacing(3)

        # Вторая колонка с полями растягивается
        detail_layout.setColumnStretch(1, 1)

        self.detail_fields = {}

        for row, (label_text, _key) in enumerate(DETAIL_FIELDS):
            label = QLabel(label_text + ":")
            label.setMinimumWidth(55)

            detail_layout.addWidget(label, row, 0)

            edit = QLineEdit()
            edit.setReadOnly(True)

            # Уменьшаем высоту поля
            edit.setFixedHeight(24)

            detail_layout.addWidget(edit, row, 1)

            self.detail_fields[label_text] = edit

        # --- Objects -----------------------------------------------------

        objects_row = len(DETAIL_FIELDS)

        objects_label = QLabel("Objects:")
        objects_label.setMinimumWidth(55)

        detail_layout.addWidget(
            objects_label,
            objects_row,
            0
        )

        objects_edit = QLineEdit()
        objects_edit.setReadOnly(True)

        # Уменьшаем высоту поля
        objects_edit.setFixedHeight(24)

        detail_layout.addWidget(
            objects_edit,
            objects_row,
            1
        )

        self.detail_fields["Objects"] = objects_edit

        # --- Descr -------------------------------------------------------

        descr_row = objects_row + 1

        descr_label = QLabel("Descr:")
        descr_label.setAlignment(Qt.AlignTop)

        detail_layout.addWidget(
            descr_label,
            descr_row,
            0
        )

        self.descr_view = QPlainTextEdit()
        self.descr_view.setReadOnly(True)

        # Минимальная высота описания,
        # но при увеличении панели оно всё равно может растягиваться
        self.descr_view.setMinimumHeight(70)

        detail_layout.addWidget(
            self.descr_view,
            descr_row,
            1
        )

        # Только описание получает дополнительное свободное место
        detail_layout.setRowStretch(descr_row, 1)

        # --- Добавляем панель атрибутов в вертикальный splitter ---------

        splitter.addWidget(detail_box)

        # По умолчанию больше места отдаём дереву MIB
        splitter.setSizes([600, 250])

        # Не даём верхней части с поиском и фильтрами полностью исчезнуть
        tree_box.setMinimumHeight(250)

        # Минимальная высота панели атрибутов
        detail_box.setMinimumHeight(200)

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
            /* ---- базовые ---- */
            QWidget {
                font-family: "Segoe UI", "Inter", system-ui, sans-serif;
                font-size: 13px;
                color: #1f2937;
            }
            QMainWindow {
                background: #f4f5f7;
            }
            QStatusBar {
                background: #eef0f3;
                color: #6b7280;
                border-top: 1px solid #e5e7eb;
            }
            QMenuBar {
                background: #eef0f3;
                color: #1f2937;
                border-bottom: 1px solid #e5e7eb;
                padding: 2px 0;
            }
            QMenuBar::item {
                padding: 4px 10px;
                background: transparent;
            }
            QMenuBar::item:selected {
                background: #e5e7eb;
                border-radius: 4px;
            }
            QMenu {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 24px 6px 12px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #f3f4f6;
            }

            /* ---- группы ---- */
            QGroupBox {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px 10px 10px 10px;
                font-weight: 600;
                color: #374151;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #4b5563;
                background: #ffffff;
            }

            /* ---- поля ввода ---- */
            QLineEdit, QComboBox {
                background: #ffffff;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                padding: 5px 8px;
                selection-background-color: #dbeafe;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #93c5fd;
            }
            QLineEdit:disabled {
                background: #f9fafb;
                color: #9ca3af;
            }
            
            QComboBox {
    background: #ffffff;
    color: #000000;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #000000;
    border: 1px solid #d1d5db;
    outline: none;
    selection-background-color: #dbeafe;
    selection-color: #000000;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 2px 6px;
}
            /* ---- компактные галочки фильтра дерева MIB ---- */

QGroupBox#treeTypeFilter QCheckBox {
    spacing: 5px;
}


QGroupBox#treeTypeFilter QCheckBox::indicator {
    width: 13px;
    height: 13px;
    border-radius: 3px;
}
            /* ---- кнопки ---- */
            QPushButton {
                background: #ffffff;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                padding: 6px 14px;
                min-height: 26px;
                color: #374151;
            }
            QPushButton:hover {
                background: #f9fafb;
                border-color: #9ca3af;
            }
            QPushButton:pressed {
                background: #f3f4f6;
            }
            QPushButton:disabled {
                background: #f3f4f6;
                color: #9ca3af;
                border-color: #e5e7eb;
            }

            /* главная кнопка запуска */
            QPushButton#primaryButton {
                background: #2563eb;
                border: 1px solid #1d4ed8;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton#primaryButton:hover {
                background: #1d4ed8;
                border-color: #1e40af;
            }
            QPushButton#primaryButton:pressed {
                background: #1e40af;
            }
            QPushButton#primaryButton:disabled {
                background: #93c5fd;
                border-color: #93c5fd;
                color: #eff6ff;
            }

            /* ---- чекбоксы ---- */
            QCheckBox {
                spacing: 8px;
                color: #374151;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #2563eb;
                border-color: #2563eb;
            }
            QCheckBox::indicator:hover {
                border-color: #93c5fd;
            }

            /* ---- вкладки ---- */
            QTabWidget::pane {
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                background: transparent;
                border: none;
                padding: 8px 16px;
                margin-right: 2px;
                color: #6b7280;
                border-bottom: 2px solid transparent;
            }
            QTabBar::tab:selected {
                color: #1f2937;
                border-bottom: 2px solid #2563eb;
                font-weight: 600;
            }
            QTabBar::tab:hover:!selected {
                color: #374151;
                background: #f9fafb;
                border-radius: 4px 4px 0 0;
            }

            /* ---- лог ---- */
            QPlainTextEdit {
                background: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #374151;
                border-radius: 6px;
                padding: 4px;
                selection-background-color: #3b82f6;
                font-family: "Consolas", "Cascadia Code", "Courier New", monospace;
                font-size: 12px;
            }

            /* ---- таблицы / дерево ---- */

QTableWidget, QTreeWidget {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    gridline-color: #f3f4f6;
    outline: none;
}


/* Элементы дерева */

QTreeWidget::item {
    padding: 4px 6px;
}


/* Выбранный объект */

QTreeWidget::item:selected {
    background: #dbeafe;
    color: #1e3a8a;
    border: none;
}


/* Объект, когда дерево активно */

QTreeWidget::item:selected:active {
    background: #bfdbfe;
    color: #1e3a8a;
}


/* Убираем рамку фокуса */

QTreeWidget::item:focus {
    outline: none;
    border: none;
}
            QHeaderView::section {
                background: #f9fafb;
                color: #4b5563;
                border: none;
                border-bottom: 1px solid #e5e7eb;
                border-right: 1px solid #f3f4f6;
                padding: 6px 8px;
                font-weight: 600;
            }
            

            /* ---- сплиттер ---- */
            QSplitter::handle {
                background: #e5e7eb;
            }
            QSplitter::handle:horizontal {
                width: 3px;
            }
            QSplitter::handle:vertical {
                height: 3px;
            }
            QSplitter::handle:hover {
                background: #93c5fd;
            }

            /* ---- скроллбары (минималистичные) ---- */
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #d1d5db;
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #9ca3af;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar:horizontal {
                background: transparent;
                height: 10px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #d1d5db;
                border-radius: 5px;
                min-width: 30px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #9ca3af;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
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

        detect_unsafe = self.check_detect_unsafe.isChecked()
        self.thread = PipelineThread(input_dir, output_dir, selected_keywords, detect_unsafe)
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
        self._last_df = None
        self.mib_tree.clear()
        self._clear_detail_fields()
        self._refresh_module_combo(None)

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
    def _get_selected_tree_types(self):
        return {
            _normalize_type_name(type_name)
            for type_name, cb in self.tree_type_checkboxes.items()
            if cb.isChecked()
        }

    def _on_tree_type_filter_changed(self, _state):
        self._rebuild_mib_tree()
        self._apply_tree_search_filter(self.tree_search_edit.text())

    def _rebuild_mib_tree(self):
        """Полностью перестраивает дерево из self._last_df с учётом текущих
        отметок фильтра типов. Нужен полный ребилд (а не просто скрытие
        узлов), потому что иерархия должна строиться заново среди ТОЛЬКО
        выбранных типов — иначе объект, чей "родитель" по OID отфильтрован,
        должен подняться на уровень выше (или стать корнем)."""
        self.mib_tree.clear()
        self._clear_detail_fields()

        df = self._last_df
        if df is None or len(df) == 0:
            return

        selected_types = self._get_selected_tree_types()
        if not selected_types:
            return

        records = df.to_dict(orient="records")

        valid_records = []
        selected_module = self._get_selected_module()
        for rec in records:

            oid_str = rec.get("oid")
            if not oid_str or not isinstance(oid_str, str):
                continue
            if selected_module is not None and str(rec.get("file_name") or "") != selected_module:
                continue
            try:
                oid_tuple = tuple(int(part) for part in oid_str.strip().split("."))
            except ValueError:
                continue
            if not oid_tuple:
                continue
            if _normalize_type_name(rec.get("class")) not in selected_types:
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

            items_by_oid.setdefault(oid_tuple, item)

        self.mib_tree.expandToDepth(1)

    def _populate_mib_tree(self, df):
        self._last_df = df
        self._refresh_module_combo(df)
        self._rebuild_mib_tree()
        self._apply_tree_search_filter(self.tree_search_edit.text())

    def _apply_tree_search_filter(self, text):
        """Показывает только узлы, чьё имя (или имя кого-то из потомков)
        содержит текст поиска. Родительские узлы совпавших элементов
        остаются видимыми (и раскрываются), чтобы путь был понятен."""
        needle = text.strip().lower()

        def _filter_item(item):
            rec = item.data(0, Qt.UserRole) or {}
            name = str(rec.get("name") or "").lower()
            oid = str(rec.get("oid") or "").lower()
            self_match = (not needle) or (needle in name) or (needle in oid)
            child_match = False
            for i in range(item.childCount()):
                if _filter_item(item.child(i)):
                    child_match = True
            visible = self_match or child_match
            item.setHidden(not visible)
            if needle and child_match:
                item.setExpanded(True)
            return visible

        root = self.mib_tree.invisibleRootItem()
        for i in range(root.childCount()):
            _filter_item(root.child(i))

    def _on_tree_selection_changed(self, current, _previous):
        if current is None:
            self._clear_detail_fields()
            return

        rec = current.data(0, Qt.UserRole)
        if not rec:
            self._clear_detail_fields()
            return

        for label_text, key in DETAIL_FIELDS:
            self.detail_fields[label_text].setText(_format_detail_value(rec.get(key)))

        self.detail_fields["Objects"].setText(_format_detail_value(rec.get("objects")))
        self.descr_view.setPlainText(_format_detail_value(rec.get("description")))

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