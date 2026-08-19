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


_TYPE_DEF_RE = re.compile(r"^\s*([a-z][A-Za-z0-9-]*)(?:\s+MACRO)?\s*::=\s*(?!\{)", re.MULTILINE)

_QUOTED_STRING_RE = re.compile(r'"[^"]*"', re.DOTALL)


def _build_known_correct_names(text):
    """Собирает {имя.lower(): оригинальное_написание} по всем идентификаторам,
    хотя бы раз встретившимся с заглавной буквы — вне текста внутри кавычек."""
    code_only = _QUOTED_STRING_RE.sub('""', text)
    names = {}
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9-]*)\b", code_only):
        name = match.group(1)
        names.setdefault(name.lower(), name)
    return names


def _fix_type_definition_case(text):
    """Правит регистр в объявлениях типов (см. _TYPE_DEF_RE выше)."""
    correct_names = _build_known_correct_names(text)
    fixes = []

    def _replace(match):
        declared_name = match.group(1)
        correct_name = correct_names.get(declared_name.lower())
        has_reference = bool(correct_name) and correct_name != declared_name
        if not has_reference:
            fallback_name = declared_name[0].upper() + declared_name[1:]
            if fallback_name == declared_name:
                return match.group(0)
            correct_name = fallback_name
        fixes.append((declared_name, correct_name, has_reference))
        return match.group(0).replace(declared_name, correct_name, 1)

    fixed_text = _TYPE_DEF_RE.sub(_replace, text)
    return fixed_text, fixes


_MODULE_HEADER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)(\s+DEFINITIONS\b)", re.MULTILINE)

_UNICODE_SPACES = {
    "\u00a0": " ",
    "\u2007": " ",
    "\u2009": " ",
    "\u200b": "",
    "\ufeff": "",
}

_SMART_QUOTES = {
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
}


def _fix_module_header_case(text, mib_name):
    match = _MODULE_HEADER_RE.search(text)
    if not match:
        return text, []
    declared_name = match.group(1)
    if declared_name == mib_name or declared_name.lower() != mib_name.lower():
        return text, []
    start, end = match.span(1)
    fixed_text = text[:start] + mib_name + text[end:]
    return fixed_text, [(declared_name, mib_name)]


def _fix_unicode_whitespace(text):
    fixes = []
    for bad_char, replacement in _UNICODE_SPACES.items():
        count = text.count(bad_char)
        if count:
            fixes.append(f"U+{ord(bad_char):04X} x{count}")
            text = text.replace(bad_char, replacement)
    return text, fixes


def _fix_smart_quotes(text):
    fixes = []
    for bad_char, replacement in _SMART_QUOTES.items():
        count = text.count(bad_char)
        if count:
            fixes.append(f"'{bad_char}' x{count}")
            text = text.replace(bad_char, replacement)
    return text, fixes


_TRAILING_COMMA_RE = re.compile(r",(\s*)\}")


def _fix_trailing_comma(text):
    count = len(_TRAILING_COMMA_RE.findall(text))
    if not count:
        return text, []
    fixed_text = _TRAILING_COMMA_RE.sub(r"\1}", text)
    return fixed_text, [f"висячая запятая перед '}}' x{count}"]


# -----------------------------------------------------------------------
# Удаление комментариев для диагностики "непарных кавычек/скобок" — иначе
# закомментированные (через "--") старые куски определений с забытыми
# кавычками/скобками ложно засчитываются как реальные проблемы файла.
# Комментарий SMI начинается с "--" и заканчивается следующим "--" либо
# концом строки, в зависимости от того, что раньше. При этом "--" внутри
# строкового литерала (в кавычках) комментарием не считается — поэтому
# состояние "внутри строки" отслеживается отдельно и имеет приоритет.
# -----------------------------------------------------------------------
def _strip_comments(text):
    result = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            result.append(ch)
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            j = i + 2
            ended_by_dashes = False
            while j < n:
                if text[j] == "\n":
                    break
                if text[j] == "-" and j + 1 < n and text[j + 1] == "-":
                    j += 2
                    ended_by_dashes = True
                    break
                j += 1
            else:
                j = n
            if not ended_by_dashes and j < n and text[j] == "\n":
                result.append("\n")
                i = j + 1
            else:
                i = j
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _detect_unsafe_issues(text):
    """Детектирует проблемы, которые НЕ чинятся автоматически (риск испортить
    реальный текст) — только предупреждение в лог, чтобы проверили руками.
    Комментарии (после "--") из проверки исключаются: закомментированный
    старый код с забытыми кавычками/скобками — не реальная проблема файла."""
    code_only = _strip_comments(text)

    warnings = []
    quote_count = code_only.count('"')
    if quote_count % 2 != 0:
        warnings.append(
            f"нечётное количество кавычек \" ({quote_count}) вне комментариев — "
            f"похоже, где-то не закрыта строка (DESCRIPTION и т.п.); файл не "
            f"тронут, нужна ручная проверка"
        )

    code_no_strings = _QUOTED_STRING_RE.sub('""', code_only)
    open_braces = code_no_strings.count("{")
    close_braces = code_no_strings.count("}")
    if open_braces != close_braces:
        warnings.append(
            f"не совпадает число '{{' ({open_braces}) и '}}' ({close_braces}) "
            f"вне комментариев — похоже на незакрытый блок; файл не тронут, "
            f"нужна ручная проверка"
        )
    return warnings


def sanitize_mib_text(text, mib_name, detect_unsafe_issues=True):
    """Прогоняет текст MIB через все безопасные автофиксы и (опционально)
    диагностику непарных кавычек/скобок.
    Возвращает (исправленный_текст, fixes, warnings), где:
      fixes    — список строк вида "<категория>: было -> стало" (что поправили)
      warnings — список строк с проблемами, которые не тронули (нужна ручная
                 проверка); пустой список, если detect_unsafe_issues=False.
    Ничего не пишет на диск — работает только с текстом в памяти."""
    fixes = []

    text, unicode_fixes = _fix_unicode_whitespace(text)
    for f in unicode_fixes:
        fixes.append(f"юникод-пробел: {f}")

    text, quote_fixes = _fix_smart_quotes(text)
    for f in quote_fixes:
        fixes.append(f"типографская кавычка: {f}")

    text, comma_fixes = _fix_trailing_comma(text)
    fixes.extend(f"грамматика: {f}" for f in comma_fixes)

    text, header_fixes = _fix_module_header_case(text, mib_name)
    for old, new in header_fixes:
        fixes.append(f"регистр заголовка модуля: '{old}' -> '{new}'")

    text, type_def_fixes = _fix_type_definition_case(text)
    for old, new, has_reference in type_def_fixes:
        if has_reference:
            fixes.append(f"регистр объявления типа: '{old}' -> '{new}'")
        else:
            fixes.append(
                f"регистр объявления типа (эталон не найден, поднята только "
                f"первая буква): '{old}' -> '{new}'"
            )

    warnings = _detect_unsafe_issues(text) if detect_unsafe_issues else []

    return text, fixes, warnings


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


def find_files_with_any_keyword(directory, keywords):
    """Ищет .mib файлы, которые содержат хотя бы одно из выбранных ключевых слов."""
    if not keywords:
        return []

    input_files = []
    patterns = [re.compile(r'\b' + re.escape(keyword) + r'\b', re.IGNORECASE)
                for keyword in keywords]

    for root, _dirs, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(".mib"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    if any(pattern.search(content) for pattern in patterns):
                        file_name = os.path.splitext(file)[0]
                        input_files.append(file_name)
                except Exception as e:
                    raise PipelineError(f"Ошибка при чтении файла {file_path}: {e}")
    return input_files


def find_mib_path(mib_name, dirs):
    target = (mib_name + ".mib").lower()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fname in files:
                if fname.lower() == target:
                    return os.path.join(root, fname)
    return None


def read_mib_from_dirs(mib_name, dirs, fixes_log=None, warnings_log=None, detect_unsafe_issues=True):
    path = find_mib_path(mib_name, dirs)
    if path is None:
        searched = ", ".join(dirs)
        raise PySmiReaderFileNotFoundError(
            f"MIB '{mib_name}.mib' не найден ни в одной из папок (включая подпапки): {searched}",
            reader=None,
        )
    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()

    fixed_content, fixes, warnings = sanitize_mib_text(content, mib_name, detect_unsafe_issues)
    if fixes and fixes_log is not None:
        for fix_description in fixes:
            fixes_log.append((path, fix_description))
    if warnings and warnings_log is not None:
        for warning_text in warnings:
            warnings_log.append((path, warning_text))

    return fixed_content


def _format_syntax(syntax):
    """Строит человекочитаемое представление SYNTAX из JSON-описания pysmi:
    базовый тип + ограничения (enumeration или range), если есть."""
    if not isinstance(syntax, dict):
        return ""
    base_type = syntax.get("type", "") or ""
    constraints = syntax.get("constraints") or {}

    enumeration = constraints.get("enumeration")
    if enumeration:
        items = []
        for key, val in enumeration.items():
            key_str = str(key)
            if key_str.lstrip("-").isdigit():
                items.append(f"{val}({key_str})")
            else:
                items.append(f"{key_str}({val})")
        return f"{base_type} {{{', '.join(items)}}}"

    range_constraint = constraints.get("range")
    if range_constraint:
        try:
            parts = []
            for r in range_constraint:
                if isinstance(r, dict):
                    parts.append(f"{r.get('min')}..{r.get('max')}")
                else:
                    parts.append(str(r))
            return f"{base_type} (SIZE({', '.join(parts)}))"
        except Exception:
            pass

    return base_type


def _format_defval(defval):
    """Приводит DEFVAL из JSON-описания pysmi к простой строке."""
    if defval is None:
        return ""
    if isinstance(defval, dict):
        return str(defval.get("value", defval))
    return str(defval)


def _format_indices(indices):
    """Приводит INDEX/AUGMENTS из JSON-описания pysmi к строке имён через запятую."""
    if not indices:
        return ""
    names = []
    for idx in indices:
        if isinstance(idx, dict):
            names.append(idx.get("object", ""))
        else:
            names.append(str(idx))
    return ", ".join(name for name in names if name)


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
                "maxaccess": value.get("maxaccess", "") or "",
                "status": value.get("status", "") or "",
                "syntax_display": _format_syntax(value.get("syntax")),
                "defval_display": _format_defval(value.get("defval")),
                "indices_display": _format_indices(value.get("indices")),
            }
        )
    return parsed_data


def _save_to_csv_buffer(parsed_data):
    f = io.StringIO()
    fieldnames = [
        "file_name", "class", "nodetype", "name", "oid", "objects", "enumeration",
        "maxaccess", "status", "syntax_display", "defval_display", "indices_display",
    ]
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
                "maxaccess": item["maxaccess"],
                "status": item["status"],
                "syntax_display": item["syntax_display"],
                "defval_display": item["defval_display"],
                "indices_display": item["indices_display"],
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
    """Обрабатывает .mib файлы из нескольких папок (рекурсивно) для поиска NOTIFICATION-TYPE."""
    all_results = []
    processed_names = set()

    for directory_path in dirs:
        if not os.path.isdir(directory_path):
            continue
        for root, _dirs, files in os.walk(directory_path):
            for mib_file in files:
                if not mib_file.lower().endswith(".mib"):
                    continue
                dedup_key = mib_file.lower()
                if dedup_key in processed_names:
                    continue
                processed_names.add(dedup_key)
                file_path = os.path.join(root, mib_file)
                try:
                    results = parse_mib_file(file_path)
                    all_results.extend(results)
                except Exception:
                    pass
    return all_results


def run_pipeline(input_dir, output_dir, log_callback=None, keywords=None, detect_unsafe_issues=True):
    """
    параметр keywords: список строк, например ["NOTIFICATION-TYPE", "OBJECT-TYPE"]
    параметр detect_unsafe_issues: включает/выключает диагностику непарных
        кавычек/скобок в MIB-файлах (см. sanitize_mib_text/_detect_unsafe_issues)
    """
    if keywords is None:
        keywords = ["NOTIFICATION-TYPE"]

    def log(msg):
        if log_callback:
            log_callback(msg)

    if not os.path.isdir(input_dir):
        raise PipelineError(f"Папка с исходными MIB не найдена: {input_dir}")

    os.makedirs(output_dir, exist_ok=True)

    mib_dirs = [input_dir]

    total_mib_count = sum(
        1
        for _root, _dirs, files in os.walk(input_dir)
        for f in files
        if f.lower().endswith(".mib")
    )
    log(f"Всего .mib файлов видно в '{input_dir}' (со всеми подпапками): {total_mib_count}")

    keywords_str = ", ".join(keywords)
    log(f"Поиск файлов с типами: {keywords_str} (рекурсивно по всем подпапкам)...")

    input_files = find_files_with_any_keyword(input_dir, keywords)

    log(f"Найдено файлов: {len(input_files)}")
    if input_files:
        log(str(input_files))
    else:
        log("Предупреждение: файлы по выбранным типам не найдены!")

    if not input_files:
        raise PipelineError(
            f"Не найдено ни одного .mib файла с выбранными типами ({keywords_str}) в {input_dir}"
        )

    dst_directory = os.path.join(output_dir, "output")
    if os.path.isdir(dst_directory):
        shutil.rmtree(dst_directory)
    os.makedirs(dst_directory, exist_ok=True)

    mib_compiler = MibCompiler(
        SmiV1CompatParser(),
        JsonCodeGen(),
        FileWriter(dst_directory),
    )
    sanitizer_fixes = []
    sanitizer_warnings = []
    mib_compiler.add_sources(
        CallbackReader(
            lambda m, c: read_mib_from_dirs(
                m, mib_dirs, sanitizer_fixes, sanitizer_warnings, detect_unsafe_issues
            )
        )
    )
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

    if sanitizer_fixes:
        log(
            f"Автоматически исправлено {len(sanitizer_fixes)} проблем в MIB-текстах "
            f"перед компиляцией (файлы на диске не менялись):"
        )
        for path, fix_description in sanitizer_fixes:
            log(f"    {path}: {fix_description}")

    if not detect_unsafe_issues:
        log("Проверка потенциальных проблем (непарные кавычки/скобки) отключена.")
    elif sanitizer_warnings:
        log(
            f"Обнаружено {len(sanitizer_warnings)} потенциальных проблем, которые НЕ "
            f"были исправлены автоматически (нужна ручная проверка):"
        )
        for path, warning_text in sanitizer_warnings:
            log(f"    {path}: {warning_text}")

    log(f"Результаты компиляции: {', '.join(f'{x}:{results[x]}' for x in results)}")

    compiled_count = sum(1 for v in results.values() if str(v) == "compiled")

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
        return name.strip().lower().removesuffix(".mib")

    matched_mib_names = {_normalize_mib_name(mib_name) for mib_name, _, _ in errors_found}

    success_statuses = {"compiled", "untouched"}
    for mib_name, status in results.items():
        status_str = str(status)
        if status_str in success_statuses:
            continue
        if _normalize_mib_name(mib_name) in matched_mib_names:
            continue
        fallback_line = f"failing with status '{status_str}' at MIB {mib_name}, line ?"
        log(fallback_line)
        errors_found.append((mib_name, "?", fallback_line + " (детальная причина не найдена)"))

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
    if errors_found or (detect_unsafe_issues and sanitizer_warnings):
        error_log_path = os.path.join(output_dir, "mib_compile_errors.log")
        with open(error_log_path, "w", encoding="utf-8") as err_file:
            if errors_found:
                err_file.write(f"=== Ошибки компиляции ({len(errors_found)}) ===\n")
                for mib_name, line_no, raw_log_line in errors_found:
                    mib_file_path = find_mib_path(mib_name, mib_dirs) or (
                        f"{mib_name}.mib (не найден)"
                    )
                    err_file.write("\n")
                    err_file.write(f"{datetime.datetime.now()} \n")
                    err_file.write(f"Файл: {mib_file_path}, строка: {line_no}\n")
                    err_file.write(f"Исходный лог: {raw_log_line}\n")

            if detect_unsafe_issues and sanitizer_warnings:
                err_file.write(
                    f"\n\n=== Потенциальные проблемы, не исправленные автоматически "
                    f"({len(sanitizer_warnings)}) ===\n"
                )
                for path, warning_text in sanitizer_warnings:
                    err_file.write("\n")
                    err_file.write(f"Файл: {path}\n")
                    err_file.write(f"{warning_text}\n")

        log(f"Ошибки/предупреждения сохранены в: {error_log_path}")
    else:
        log("Ошибок компиляции не обнаружено.")

    stats = {
        "found": len(input_files),
        "compiled": compiled_count,
        "errors": len(errors_found),
        "objects": len(df),
        "notifications": len(notification_results),
        "sanitizer_fixes": len(sanitizer_fixes),
        "sanitizer_warnings": len(sanitizer_warnings),
    }

    return {
        "stats": stats,
        "df": df,
        "output_csv": output_csv,
        "error_log": error_log_path,
        "output_dir": output_dir,
        "errors_found": errors_found,
        "sanitizer_fixes": sanitizer_fixes,
        "sanitizer_warnings": sanitizer_warnings,
    }