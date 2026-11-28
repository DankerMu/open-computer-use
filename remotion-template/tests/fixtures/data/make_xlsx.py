# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
#
# Test helper for kit-data-xlsx.test.mjs; runs as `python3 -I make_xlsx.py`.
#
#   make_xlsx.py <out.xlsx>                 reads a JSON spec on stdin
#   make_xlsx.py --inspect <file> <sheet> <cell>
#       prints {"formula": [value, data_type], "saved": [value, data_type]}
#       from a data_only=False and a data_only=True load
#
# Spec: {"sheets": [{"title", "rows", "merged", "validation"} |
#                   {"title", "chart": true}]}
# "rows" is a list of rows from sheet row 1, each a list of cells from
# column A. A cell is null (no cell), a JSON string, number or boolean
# written by openpyxl, or an object:
#   {"value": <value>, "date": "<ISO datetime>", "format": "<number_format>",
#    "formula": "<formula text>", "saved": "<v text>", "type": "<t attribute>",
#    "format_id": <raw numFmtId>}
# openpyxl writes "value" (or the datetime "date") with "format". When any of
# "formula", "saved", "type" or "format_id" is present, the cell's element in
# the saved sheet XML is rewritten as <c r s t><f/><v/></c>: openpyxl writes
# no saved value for a formula and writes floats with %.16g, and has no way
# to put a raw built-in format id on a style. "format_id" appends an xf with
# that numFmtId to styles.xml. "validation" appends a data-validation extLst
# to the sheet XML. "chart" makes a chart sheet holding a bar chart of the
# first worksheet.

import datetime
import json
import re
import sys
import zipfile
from xml.sax.saxutils import escape

import openpyxl
from openpyxl.chart import BarChart, Reference

RAW_KEYS = ("formula", "saved", "type", "format_id")
VALIDATION = (
    '<extLst><ext uri="{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}"'
    ' xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
    '<x14:dataValidations count="1" xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main">'
    '<x14:dataValidation type="list" allowBlank="1"><x14:formula1><xm:f>$A$1:$A$2</xm:f></x14:formula1>'
    "<xm:sqref>B2</xm:sqref></x14:dataValidation></x14:dataValidations></ext></extLst>"
)


def build(spec, out):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    raw = {}  # sheet title -> {coordinate: cell spec}
    for sheet in spec["sheets"]:
        if sheet.get("chart"):
            chart = BarChart()
            chart.add_data(Reference(wb.worksheets[0], min_col=2, min_row=1, max_row=3), titles_from_data=True)
            wb.create_chartsheet(sheet["title"]).add_chart(chart)
            continue
        ws = wb.create_sheet(sheet["title"])
        raw[ws.title] = {}
        for row_number, row in enumerate(sheet.get("rows", []), 1):
            for column, item in enumerate(row, 1):
                if item is None:
                    continue
                cell = ws.cell(row=row_number, column=column)
                if not isinstance(item, dict):
                    cell.value = item
                    continue
                if "date" in item:
                    cell.value = datetime.datetime.fromisoformat(item["date"])
                else:
                    cell.value = item.get("value", 0)
                if "format" in item:
                    cell.number_format = item["format"]
                if any(key in item for key in RAW_KEYS):
                    raw[ws.title][cell.coordinate] = item
        for merged in sheet.get("merged", []):
            ws.merge_cells(merged)
    wb.save(out)
    rewrite(out, wb, spec, raw)


def rewrite(out, wb, spec, raw):
    with zipfile.ZipFile(out) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    styles = parts["xl/styles.xml"].decode("utf-8")
    style_of = {}  # raw numFmtId -> appended xf index

    def style_index(format_id):
        nonlocal styles
        if format_id not in style_of:
            match = re.search(r'<cellXfs count="(\d+)">', styles)
            count = int(match.group(1))
            xf = f'<xf numFmtId="{format_id}" fontId="0" fillId="0" borderId="0" applyNumberFormat="1" xfId="0"/>'
            styles = styles.replace(match.group(0), f'<cellXfs count="{count + 1}">', 1)
            styles = styles.replace("</cellXfs>", f"{xf}</cellXfs>", 1)
            style_of[format_id] = count
        return style_of[format_id]

    for sheet in spec["sheets"]:
        if sheet.get("chart"):
            continue
        ws = wb[sheet["title"]]
        name = ws.path.lstrip("/")
        xml = parts[name].decode("utf-8")
        for coordinate, item in raw[ws.title].items():
            element = re.compile(rf'<c r="{coordinate}"( [^>/]*)?(/>|>.*?</c>)', re.DOTALL)
            found = element.search(xml)
            style = re.search(r' s="(\d+)"', found.group(1) or "")
            if "format_id" in item:
                attributes = f' s="{style_index(item["format_id"])}"'
            else:
                attributes = f' s="{style.group(1)}"' if style else ""
            if "type" in item:
                attributes += f' t="{item["type"]}"'
            # Only a style or type change keeps the value openpyxl wrote.
            body = found.group(2)[1:-4] if found.group(2) != "/>" else ""
            if "formula" in item or "saved" in item:
                body = f"<f>{escape(item['formula'])}</f>" if "formula" in item else ""
                if "saved" in item:
                    body += f"<v>{escape(item['saved'])}</v>"
            replacement = f'<c r="{coordinate}"{attributes}>{body}</c>'
            xml = xml[: found.start()] + replacement + xml[found.end() :]
        if sheet.get("validation"):
            xml = xml.replace("</worksheet>", f"{VALIDATION}</worksheet>", 1)
        parts[name] = xml.encode("utf-8")
    parts["xl/styles.xml"] = styles.encode("utf-8")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def inspect(path, sheet, coordinate):
    result = {}
    for key, data_only in (("formula", False), ("saved", True)):
        cell = openpyxl.load_workbook(path, data_only=data_only)[sheet][coordinate]
        result[key] = [cell.value, cell.data_type]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1] == "--inspect":
        inspect(*sys.argv[2:5])
    else:
        build(json.load(sys.stdin), sys.argv[1])
