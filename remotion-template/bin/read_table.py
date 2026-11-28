# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
#
# The only reader of a source table file. `kit data` runs it as
# `python3 -I read_table.py <real path> [<sheet>]`. On success it prints the
# table object (format version 1) as JSON on stdout and exits 0; on a refusal
# it prints one line `refusal: <text>` on stderr and exits 1. kit.mjs checks
# the object's shape and writes data/<table>.json. The real path's extension,
# case-insensitive, picks the format: .xlsx is read with openpyxl, imported
# only on that path; anything else is CSV, read with the standard library
# alone. Needs Python 3.10 or later. Both streams are written as UTF-8 bytes,
# since -I ignores PYTHONIOENCODING. A CSV is streamed: it is hashed as it is
# read, only the header and the first MAX_DATA_ROWS data rows are kept, and
# the rest are counted, so memory does not grow with the number of rows; one
# line is still held whole. An xlsx is read into memory once, hashed, and
# loaded from those bytes.

import csv
import datetime
import decimal
import hashlib
import io
import json
import os
import re
import sys
import warnings

MAX_COLUMNS = 12
MAX_DATA_ROWS = 50
MAX_SIGNIFICANT_DIGITS = 15
# ASCII digits only: leading zeros (007), a short group (1,26) and a bare
# point (12.) stay text.
NUMBER = re.compile(r"-?(0|[1-9][0-9]{0,2}(,[0-9]{3})+|[1-9][0-9]*)(\.[0-9]+)?%?")
# xlsx number formats: the accepted built-in ids, and the accepted custom
# format strings (General, 0, 0.0…, #,##0, #,##0.0…, 0%, 0.0…%).
BUILTIN_NUMBER_FORMATS = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%"}
FIRST_CUSTOM_FORMAT = 164
CUSTOM_NUMBER_FORMAT = re.compile(r"General|(#,##)?0(\.0+)?|0(\.0+)?%")
DATE_TYPES = (datetime.datetime, datetime.date, datetime.time, datetime.timedelta)


class Refusal(Exception):
    pass


class HashingReader(io.RawIOBase):
    """Reads a binary file and hashes every byte it hands on."""

    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.raw.readinto(buffer)
        self.digest.update(memoryview(buffer)[:count])
        return count


class Lines:
    """Iterates the lines of a text stream; done is True once it has ended."""

    def __init__(self, text):
        self.text = text
        self.done = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.text.readline()
        if line == "":
            self.done = True
            raise StopIteration
        return line


def number_cell(number, unit, cell):
    """{value, text, unit} for a plain decimal text; cell names it in the
    digit refusal."""
    # Leading zeros are not significant; trailing zeros are.
    significant = number.lstrip("-").replace(".", "").lstrip("0")
    if len(significant) > MAX_SIGNIFICANT_DIGITS:
        raise Refusal(f"{cell}: more than {MAX_SIGNIFICANT_DIGITS} significant digits")
    value = float(number) if "." in number else int(number)
    # -0 and -0.0 are written as 0.
    return {"value": value if value != 0 else 0, "text": number, "unit": unit}


def parse_cell(text, cell):
    """A stripped cell: None when empty, else {value, text, unit}; cell names
    it in a refusal."""
    if text == "":
        return None
    if NUMBER.fullmatch(text) is None:
        return {"value": None, "text": text, "unit": ""}
    unit = "%" if text.endswith("%") else ""
    return number_cell(text.removesuffix("%").replace(",", ""), unit, cell)


def check_header(where, header):
    if len(header) < 2:
        raise Refusal(f"{where}: the header needs a label column and at least one data column")
    if len(header) > MAX_COLUMNS:
        raise Refusal(f"{where}: has {len(header)} columns; the limit is {MAX_COLUMNS}")
    first = {}
    for number, name in enumerate(header, 1):
        if name == "":
            raise Refusal(f"{where}: column {number} has an empty header")
        if name in first:
            raise Refusal(f"{where}: duplicate header {name} in columns {first[name]} and {number}")
        first[name] = number


def build_table(where, source, header, body, count, parse):
    """The table from a scanned sheet: header cells (None when there is no
    header row), the first MAX_DATA_ROWS data rows as (row number, [label,
    *cells]), and the full data row count. parse(cell, row, column header)
    gives a data cell's table entry; a short row pads with None. where
    prefixes every refusal."""
    if header is None:
        raise Refusal(f"{where}: has no header row")
    check_header(where, header)
    if count == 0:
        raise Refusal(f"{where}: has no data row")
    if count > MAX_DATA_ROWS:
        raise Refusal(f"{where}: has {count} data rows; the limit is {MAX_DATA_ROWS}")
    labels = {}
    table_rows = []
    for number, cells in body:
        if len(cells) > len(header):
            raise Refusal(f"{where}: row {number} has {len(cells)} cells; the header has {len(header)}")
        label = cells[0]
        if label == "":
            raise Refusal(f"{where}: row {number} has an empty label")
        if label in labels:
            raise Refusal(f"{where}: row {number}: duplicate label {label}, first in row {labels[label]}")
        labels[label] = number
        parsed = [parse(cell, number, column) for cell, column in zip(cells[1:], header[1:])]
        table_rows.append({"label": label, "cells": parsed + [None] * (len(header) - len(cells))})
    return {
        "version": 1,
        "source": source,
        "label_header": header[0],
        "columns": header[1:],
        "rows": table_rows,
    }


def scan_csv(path, file):
    """Reads the file to its end: (header cells or None, the first
    MAX_DATA_ROWS data rows, the data row count, sha256). A data row is
    (1-based file row, stripped cells); a fully empty row is skipped but
    still counted. A refusal is raised only once the file is read to its end,
    so undecodable bytes anywhere refuse first."""
    header = None
    body = []
    count = 0
    records = 0
    with open(path, "rb", buffering=0) as raw:
        hashing = HashingReader(raw)
        # newline="" splits lines as csv expects, on \r, \n and \r\n only,
        # and keeps a quoted line end; utf-8-sig drops a leading BOM.
        text = io.TextIOWrapper(io.BufferedReader(hashing), encoding="utf-8-sig", newline="")
        try:
            lines = Lines(text)
            try:
                for records, record in enumerate(csv.reader(lines, strict=True), 1):
                    cells = [cell.strip() for cell in record]
                    if not any(cells):
                        continue
                    if header is None:
                        header = cells
                        continue
                    count += 1
                    if count <= MAX_DATA_ROWS:
                        body.append((records, cells))
            except csv.Error as error:
                # strict=True: an unbalanced quote fails here instead of
                # swallowing every later row into one cell. Failing after the
                # last line means a quote still open at the end of the file.
                if lines.done:
                    problem = "a quoted cell is not closed"
                else:
                    problem = str(error)
                    while text.read(1 << 16):
                        pass
                raise Refusal(f"{file}: row {records + 1}: {problem}") from None
        except UnicodeDecodeError:
            raise Refusal(f"{file}: is not UTF-8; save it as CSV UTF-8") from None
    return header, body, count, hashing.digest.hexdigest()


def read_csv(path):
    file = os.path.basename(path)
    header, body, count, sha256 = scan_csv(path, file)
    source = {"file": file, "sheet": None, "sha256": sha256}

    def parse(text, row, column):
        return parse_cell(text, f"{file}: row {row}, column {column}")

    return build_table(file, source, header, body, count, parse)


def number_text(cell, where):
    """{value, text, unit} for a numeric xlsx cell, from its number format
    judged by format id (cell._style.numFmtId, as openpyxl 3.1.5 stores it)."""
    format_id = cell._style.numFmtId
    if format_id in BUILTIN_NUMBER_FORMATS:
        number_format = BUILTIN_NUMBER_FORMATS[format_id]
    elif format_id < FIRST_CUSTOM_FORMAT and cell.number_format == "General":
        # A built-in id openpyxl does not name, such as the regional date
        # formats 27-36 and 50-58, which have no format string in the file.
        raise Refusal(f"{where}: built-in number format {format_id} is not supported")
    elif format_id >= FIRST_CUSTOM_FORMAT and CUSTOM_NUMBER_FORMAT.fullmatch(cell.number_format):
        number_format = cell.number_format
    else:
        raise Refusal(f'{where}: number format "{cell.number_format}" is not supported')
    digits = decimal.Decimal(format(cell.value, ".15g"))
    unit = "%" if number_format.endswith("%") else ""
    if unit:
        digits = digits.scaleb(2)
    if not digits.is_finite():
        raise Refusal(f"{where}: more than {MAX_SIGNIFICANT_DIGITS} significant digits")
    if number_format == "General":
        if abs(digits) >= 10**MAX_SIGNIFICANT_DIGITS:
            raise Refusal(f"{where}: more than {MAX_SIGNIFICANT_DIGITS} significant digits")
        rounded = digits.normalize()
    else:
        places = len(number_format.partition(".")[2].rstrip("%"))
        # Enough precision for every digit before the point and the places.
        context = decimal.Context(prec=max(1, digits.adjusted() + 2) + places)
        rounded = digits.quantize(decimal.Decimal(1).scaleb(-places), rounding=decimal.ROUND_HALF_UP, context=context)
    if rounded == 0:
        rounded = rounded.copy_abs()
    return number_cell(format(rounded, "f"), unit, where)


def xlsx_cell(value, formula, where, data):
    """One xlsx cell from the data_only load (value) and the formula load
    (formula). A data cell gives None or {value, text, unit}; a header or
    label cell gives a string, "" when empty."""
    cell = f"{where}: cell {value.coordinate}"
    if formula.data_type == "f" and value.value is None and value.data_type == "n":
        raise Refusal(f"{cell} is a formula with no saved value; open the file in Excel and save it")
    if isinstance(value.value, DATE_TYPES):
        raise Refusal(f"{cell} holds a date or time")
    if isinstance(value.value, bool):
        raise Refusal(f"{cell} holds a boolean")
    if value.data_type == "e":
        raise Refusal(f"{cell} holds the error {value.value}")
    if value.value is None:
        # Also a formula whose saved value is the empty string.
        return None if data else ""
    if isinstance(value.value, str):
        text = value.value.strip()
        return parse_cell(text, cell) if data else text
    number = number_text(value, cell)
    return number if data else f"{number['text']}{number['unit']}"


def scan_xlsx(values, formulas, where):
    """(header strings or None, the first MAX_DATA_ROWS data rows, the data
    row count) of a worksheet, with the sheet's own row numbers; each row runs
    from column A to its last non-empty cell, and fully empty rows are
    skipped."""
    header = None
    body = []
    count = 0
    bounds = {"min_row": 1, "max_row": values.max_row, "min_col": 1, "max_col": values.max_column}
    for value_row, formula_row in zip(values.iter_rows(**bounds), formulas.iter_rows(**bounds)):
        cells = [
            xlsx_cell(value, formula, where, header is not None and column > 0)
            for column, (value, formula) in enumerate(zip(value_row, formula_row))
        ]
        while cells and cells[-1] in (None, ""):
            cells.pop()
        if not cells:
            continue
        if header is None:
            header = cells
            continue
        count += 1
        if count <= MAX_DATA_ROWS:
            body.append((value_row[0].row, cells))
    return header, body, count


def read_xlsx(path, sheet):
    file = os.path.basename(path)
    # openpyxl writes UserWarning lines to stderr (for example for a
    # data-validation extension); a refusal must start stderr.
    warnings.simplefilter("ignore")
    try:
        import openpyxl
        from openpyxl.worksheet.worksheet import Worksheet
    except ImportError:
        sys.stderr.buffer.write(b"openpyxl is not installed\n")
        sys.exit(3)
    with open(path, "rb") as source:
        data = source.read()
    try:
        formulas = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
        values = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception:
        # Not a zip, an empty zip, an encrypted file or a broken package.
        raise Refusal(f"{file}: is not a readable .xlsx file") from None
    titles = values.sheetnames
    title = titles[0] if sheet is None else sheet
    if title not in titles:
        raise Refusal(f"{file}: has no sheet {title}; its sheets are {', '.join(titles)}")
    if not isinstance(values[title], Worksheet):
        raise Refusal(f"{file}: sheet {title} is not a worksheet")
    where = f"{file}: sheet {title}"
    merged = sorted(values[title].merged_cells.ranges, key=lambda merge: (merge.min_row, merge.min_col))
    if merged:
        raise Refusal(f"{where}: range {merged[0].coord} is merged")
    header, body, count = scan_xlsx(values[title], formulas[title], where)
    source = {"file": file, "sheet": title, "sha256": hashlib.sha256(data).hexdigest()}
    return build_table(where, source, header, body, count, lambda cell, row, column: cell)


def main(argv):
    if sys.version_info < (3, 10):
        sys.stderr.buffer.write(b"read_table.py needs Python 3.10 or later\n")
        return 3
    if len(argv) not in (2, 3):
        sys.stderr.buffer.write(b"usage: python3 -I read_table.py <path> [<sheet>]\n")
        return 2
    path = argv[1]
    sheet = argv[2] if len(argv) == 3 else None
    try:
        if os.path.splitext(path)[1].lower() == ".xlsx":
            table = read_xlsx(path, sheet)
        elif sheet is not None:
            raise Refusal("--sheet applies only to .xlsx files")
        else:
            table = read_csv(path)
    except Refusal as refusal:
        sys.stderr.buffer.write(f"refusal: {refusal}\n".encode("utf-8"))
        return 1
    sys.stdout.buffer.write(f"{json.dumps(table)}\n".encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
