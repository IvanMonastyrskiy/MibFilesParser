import csv
import datetime
import io
import json
import logging
import os
import re
import shutil

import pandas as pd

from pysmi import debug
from pysmi.codegen import JsonCodeGen
from pysmi.compiler import MibCompiler
from pysmi.error import PySmiReaderFileNotFoundError
from pysmi.parser import SmiV1CompatParser
from pysmi.reader import CallbackReader
from pysmi.searcher import StubSearcher
from pysmi.writer import FileWriter

debug.set_logger(debug.Debug("compiler"))


class PipelineError(Exception):
    """Ошибка, возникшая в процессе парсинга/компиляции."""


class _CallbackLogHandler(logging.Handler):
    """Логирующий handler, который сразу отправляет строки в GUI через callback."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self.records_text = []

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.records_text.append(msg)
        if self._callback:
            self._callback(msg)


def find_files_with_keyword(directory, keyword):
    """Ищет .mib файлы в директории (рекурсивно), содержащие кодовое слово."""
    input_files = []
    for root, _dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".mib"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if keyword in content:
                        file_name = os.path.splitext(file)[0]
                        input_files.append(file_name)
                except Exception as e:
                    raise PipelineError(f"Ошибка при чтении файла {file_path}: {e}")
    return input_files


def find_mib_path(mib_name, dirs):
    """Ищет файл <mib_name>.mib рекурсивно во всех переданных директориях
    (и их произвольно вложенных подпапках), а не только на верхнем уровне."""
    target = mib_name + ".mib"
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            if target in files:
                return os.path.join(root, target)
    return None


def read_mib_from_dirs(mib_name, dirs):
    path = find_mib_path(mib_name, dirs)
    if path is None:
        searched = ", ".join(dirs)
        raise PySmiReaderFileNotFoundError(
            f"MIB '{mib_name}.mib' не найден ни в одной из папок (включая подпапки): {searched}",
            reader=None,
        )
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def parse_json_file(file_path):
    """Парсит скомпилированный JSON MIB и извлекает нужные поля."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    parsed_data = []
    for _key, value in data.items():
        if not isinstance(value, dict):
            continue
        obj_class = value.get("class")
        nodetype = value.get("nodetype")
        name = value.get("name")
        oid = value.get("oid")

        objects = []
        if "objects" in value:
            for obj in value["objects"]:
                if "object" in obj:
                    objects.append(obj["object"])

        enumeration = {}
        if "syntax" in value:
            syntax = value["syntax"]
            if "constraints" in syntax:
                constraints = syntax["constraints"]
                if "enumeration" in constraints:
                    enumeration = constraints["enumeration"]

        parsed_data.append(
            {
                "name": name,
                "class": obj_class,
                "nodetype": nodetype,
                "oid": oid,
                "objects": objects,
                "enumeration": enumeration,
            }
        )
    return parsed_data


def _save_to_csv_buffer(parsed_data):
    f = io.StringIO()
    fieldnames = ["file_name", "class", "nodetype", "name", "oid", "objects", "enumeration"]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for item in parsed_data:
        writer.writerow(
            {
                "file_name": item["file_name"],
                "class": item["class"],
                "nodetype": item["nodetype"],
                "name": item["name"],
                "oid": item["oid"],
                "objects": ", ".join(item["objects"]),
                "enumeration": str(item["enumeration"]),
            }
        )
    f.seek(0)
    return f


def process_compiled_directory(directory):
    all_parsed_data = []
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if not os.path.isfile(file_path):
            continue
        if not filename.endswith((".txt", ".json", ".mib")):
            parsed_data = parse_json_file(file_path)
            for item in parsed_data:
                item["file_name"] = filename
            all_parsed_data.extend(parsed_data)
    return _save_to_csv_buffer(all_parsed_data)


def parse_mib_file(file_path):
    """Извлекает NOTIFICATION-TYPE и их DESCRIPTION из .mib файла."""

    results = []
    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        lines = file.readlines()

    for i, line in enumerate(lines):
        if line.strip().endswith("NOTIFICATION-TYPE"):
            if line.strip() == "NOTIFICATION-TYPE":
                continue
            notification_type = line.split()[0].strip()
            description = None
            for j in range(i + 1, len(lines)):
                if lines[j].strip().startswith("DESCRIPTION"):
                    description = ""
                    k = j + 1
                    while k < len(lines):
                        if lines[k].strip().startswith('"'):
                            description += lines[k].strip()[1:]
                            break
                        else:
                            description += lines[k].strip()
                        k += 1
                    break
            if notification_type and description:
                results.append(
                    (os.path.basename(file_path), notification_type, description.strip('"'))
                )
    return results


def collect_notifications(dirs):
    """Обрабатывает .mib файлы из нескольких папок (рекурсивно, включая
    произвольно вложенные подпапки) для поиска NOTIFICATION-TYPE."""
    all_results = []
    processed_names = set()

    for directory_path in dirs:
        if not os.path.isdir(directory_path):
            continue
        for root, _dirs, files in os.walk(directory_path):
            for mib_file in files:
                if not mib_file.endswith(".mib"):
                    continue
                if mib_file in processed_names:
                    continue
                processed_names.add(mib_file)
                file_path = os.path.join(root, mib_file)
                try:
                    results = parse_mib_file(file_path)
                    all_results.extend(results)
                except Exception:
                    pass
    return all_results


def run_pipeline(input_dir, output_dir, log_callback=None, keyword="NOTIFICATION-TYPE"):
    """
    параметр input_dir: папка с исходными MIB-файлами (поиск и зависимостей,
        и самих файлов ведётся рекурсивно по всем вложенным подпапкам)
    параметр output_dir: папка, куда сохранять результаты
    параметр log_callback: функция callback(str), вызывается для каждой строки лога
    параметр keyword: кодовое слово для отбора файлов (по умолчанию NOTIFICATION-TYPE)
    возвращает dict со статистикой, DataFrame, путями к результатам
    """

    def log(msg):
        if log_callback:
            log_callback(msg)

    if not os.path.isdir(input_dir):
        raise PipelineError(f"Папка с исходными MIB не найдена: {input_dir}")

    os.makedirs(output_dir, exist_ok=True)

    # Поиск ведётся рекурсивно по всей input_dir, поэтому отдельно
    # выделять "common" (или любую другую) подпапку больше не нужно —
    # она и так попадёт в обход.
    mib_dirs = [input_dir]

    log(f"Поиск файлов с кодовым словом '{keyword}' (рекурсивно по всем подпапкам)...")
    input_files = find_files_with_keyword(input_dir, keyword)

    log(f"Найдено файлов: {len(input_files)}")
    log(str(input_files))

    if not input_files:
        raise PipelineError(
            f"Не найдено ни одного .mib файла с кодовым словом '{keyword}' в {input_dir}"
        )

    dst_directory = os.path.join(output_dir, "output")
    # На Windows FileWriter пишет через os.rename(tmp, target), а rename не
    # может перезаписать существующий файл (WinError 183). Поэтому при
    # повторном запуске в тот же output_dir компиляция "падает" на уже
    # существующих файлах и результат тихо не обновляется. Чтобы такого не
    # было, перед каждым запуском полностью очищаем папку с результатами.
    if os.path.isdir(dst_directory):
        shutil.rmtree(dst_directory)
    os.makedirs(dst_directory, exist_ok=True)

    mib_compiler = MibCompiler(
        SmiV1CompatParser(),
        JsonCodeGen(),
        FileWriter(dst_directory),
    )
    mib_compiler.add_sources(CallbackReader(lambda m, c: read_mib_from_dirs(m, mib_dirs)))
    mib_compiler.add_searchers(StubSearcher(*JsonCodeGen.baseMibs))

    capture_handler = _CallbackLogHandler(log)
    capture_handler.setLevel(logging.DEBUG)
    pysmi_logger = logging.getLogger("pysmi")
    pysmi_logger.addHandler(capture_handler)
    pysmi_logger.setLevel(logging.DEBUG)

    try:
        results = mib_compiler.compile(*input_files, noDeps=True)
    finally:
        pysmi_logger.removeHandler(capture_handler)

    log(f"Результаты компиляции: {', '.join(f'{x}:{results[x]}' for x in results)}")

    compiled_count = sum(1 for v in results.values() if str(v) == "compiled")

    # --- Сбор ошибок компиляции -------------------------------------------
    # 1) Сначала пытаемся вытащить детальную информацию (файл + номер строки)
    #    из текстового лога pysmi по известному формату сообщения.
    error_pattern = re.compile(r"failing on .*? at MIB\s+(\S+?),\s+line\s+(\d+)")
    errors_found = []
    seen = set()
    for line in capture_handler.records_text:
        match = error_pattern.search(line)
        if match:
            mib_name, line_no = match.group(1), match.group(2)
            key = (mib_name, line_no)
            if key not in seen:
                seen.add(key)
                errors_found.append((mib_name, line_no, line))

    def _normalize_mib_name(name):
        # Имя MIB в тексте лога pysmi иногда содержит расширение ".mib" и
        # может отличаться регистром от ключа в results — приводим к общему
        # виду, иначе одна и та же ошибка задвоится: один раз из regex (с
        # номером строки), второй раз как "fallback" (с "line ?").
        return name.strip().lower().removesuffix(".mib")

    matched_mib_names = {
        _normalize_mib_name(mib_name) for mib_name, _line_no, _line in errors_found
    }
    for mib_name, status in results.items():
        if str(status) == "failed" and _normalize_mib_name(mib_name) not in matched_mib_names:
            fallback_line = (
                f"failing on unknown reason at MIB {mib_name}, line ?"
            )
            log(fallback_line)
            errors_found.append(
                (
                    mib_name,
                    "?",
                    f"{fallback_line} "
                    f"(детальная причина не найдена в логе pysmi, "
                    f"проверьте компилируемый файл вручную)",
                )
            )

    log("Сбор данных из скомпилированных JSON...")
    virtual_file = process_compiled_directory(dst_directory)
    df = pd.read_csv(virtual_file, delimiter=",")

    df["objects"] = df["objects"].apply(
        lambda x: x.split(", ") if isinstance(x, str) and x.strip() else []
    )

    reverse_dependencies = {}
    for _index, row in df.iterrows():
        name = row["name"]
        for obj in row["objects"]:
            reverse_dependencies.setdefault(obj, []).append(name)

    df["depend"] = df["name"].apply(lambda x: ", ".join(reverse_dependencies.get(x, [])))

    log("Поиск NOTIFICATION-TYPE и описаний...")
    notification_results = collect_notifications(mib_dirs)

    if notification_results:
        notif_df = pd.DataFrame(
            notification_results, columns=["filename", "sub_string_text", "description"]
        )
        notif_df["file_name"] = notif_df["filename"].str.replace(".mib", "", regex=False)

        df = df.merge(
            notif_df[["file_name", "sub_string_text", "description"]],
            left_on=["file_name", "name"],
            right_on=["file_name", "sub_string_text"],
            how="left",
        )
        df.drop(columns=["sub_string_text"], inplace=True)
        df["description"] = df["description"].fillna("")
    else:
        df["description"] = ""

    output_csv = os.path.join(output_dir, "output_mib.csv")
    df.to_csv(output_csv, sep=";", index=False)
    log(f"CSV сохранён: {output_csv}")

    error_log_path = None
    if errors_found:
        error_log_path = os.path.join(output_dir, "mib_compile_errors.log")
        with open(error_log_path, "w", encoding="utf-8") as err_file:
            for mib_name, line_no, raw_log_line in errors_found:
                mib_file_path = find_mib_path(mib_name, mib_dirs) or (
                    f"{mib_name}.mib (не найден ни в {input_dir}, ни в его подпапках)"
                )
                err_file.write("\n")
                err_file.write(f"{datetime.datetime.now()} \n")
                err_file.write(f"Файл: {mib_file_path}, строка: {line_no}\n")
                err_file.write(f"Исходный лог: {raw_log_line}\n")
        log(f"Ошибки компиляции сохранены в: {error_log_path}")
    else:
        log("Ошибок компиляции не обнаружено.")

    stats = {
        "found": len(input_files),
        "compiled": compiled_count,
        "errors": len(errors_found),
        "objects": len(df),
        "notifications": len(notification_results),
    }

    return {
        "stats": stats,
        "df": df,
        "output_csv": output_csv,
        "error_log": error_log_path,
        "output_dir": output_dir,
        "errors_found": errors_found,
    }