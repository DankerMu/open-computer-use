# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
#
# The only reader of a source table file. `kit data` runs it as
# `python3 -I read_table.py <real path>`. On success it prints the table
# object (format version 1) as JSON on stdout and exits 0; on a refusal it
# prints one line `refusal: <text>` on stderr and exits 1. kit.mjs checks
# the object's shape and writes data/<table>.json. CSV only, standard
# library only; needs Python 3.10 or later. Both streams are written as
# UTF-8 bytes, since -I ignores PYTHONIOENCODING. The file is streamed: it is
# hashed as it is read, only the header and the first MAX_DATA_ROWS data rows
# are kept, and the rest are counted, so memory does not grow with the number
# of rows; one line is still held whole.

import csv
import hashlib
import io
import json
import os
import re
import sys

MAX_COLUMNS = 12
MAX_DATA_ROWS = 50
MAX_SIGNIFICANT_DIGITS = 15
# ASCII digits only: leading zeros (007), a short group (1,26) and a bare
# point (12.) stay text.
NUMBER = re.compile(r"-?(0|[1-9][0-9]{0,2}(,[0-9]{3})+|[1-9][0-9]*)(\.[0-9]+)?%?")


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


def parse_cell(text, file, row, column):
    """A stripped cell: None when empty, else {value, text, unit}."""
    if text == "":
        return None
    if NUMBER.fullmatch(text) is None:
        return {"value": None, "text": text, "unit": ""}
    unit = "%" if text.endswith("%") else ""
    number = text.removesuffix("%").replace(",", "")
    # Leading zeros are not significant; trailing zeros are.
    significant = number.lstrip("-").replace(".", "").lstrip("0")
    if len(significant) > MAX_SIGNIFICANT_DIGITS:
        raise Refusal(f"{file}: row {row}, column {column}: more than {MAX_SIGNIFICANT_DIGITS} significant digits")
    value = float(number) if "." in number else int(number)
    # -0 and -0.0 are written as 0.
    return {"value": value if value != 0 else 0, "text": number, "unit": unit}


def check_header(file, header):
    if len(header) < 2:
        raise Refusal(f"{file}: the header needs a label column and at least one data column")
    if len(header) > MAX_COLUMNS:
        raise Refusal(f"{file}: has {len(header)} columns; the limit is {MAX_COLUMNS}")
    first = {}
    for number, name in enumerate(header, 1):
        if name == "":
            raise Refusal(f"{file}: column {number} has an empty header")
        if name in first:
            raise Refusal(f"{file}: duplicate header {name} in columns {first[name]} and {number}")
        first[name] = number


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
    if header is None:
        raise Refusal(f"{file}: has no header row")
    check_header(file, header)
    if count == 0:
        raise Refusal(f"{file}: has no data row")
    if count > MAX_DATA_ROWS:
        raise Refusal(f"{file}: has {count} data rows; the limit is {MAX_DATA_ROWS}")
    labels = {}
    table_rows = []
    for number, cells in body:
        if len(cells) > len(header):
            raise Refusal(f"{file}: row {number} has {len(cells)} cells; the header has {len(header)}")
        cells = cells + [""] * (len(header) - len(cells))
        label = cells[0]
        if label == "":
            raise Refusal(f"{file}: row {number} has an empty label")
        if label in labels:
            raise Refusal(f"{file}: row {number}: duplicate label {label}, first in row {labels[label]}")
        labels[label] = number
        parsed = [parse_cell(cell, file, number, column) for cell, column in zip(cells[1:], header[1:])]
        table_rows.append({"label": label, "cells": parsed})
    return {
        "version": 1,
        "source": {"file": file, "sheet": None, "sha256": sha256},
        "label_header": header[0],
        "columns": header[1:],
        "rows": table_rows,
    }


def main(argv):
    if sys.version_info < (3, 10):
        sys.stderr.buffer.write(b"read_table.py needs Python 3.10 or later\n")
        return 3
    if len(argv) != 2:
        sys.stderr.buffer.write(b"usage: python3 -I read_table.py <path>\n")
        return 2
    try:
        table = read_csv(argv[1])
    except Refusal as refusal:
        sys.stderr.buffer.write(f"refusal: {refusal}\n".encode("utf-8"))
        return 1
    sys.stdout.buffer.write(f"{json.dumps(table)}\n".encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
