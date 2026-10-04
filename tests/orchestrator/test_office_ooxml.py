# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for bounded OOXML package validation."""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from office import ooxml


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SLIDE_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
SLIDE_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


def _xml(tag, ns, text=None, attrib=None, children=()):
    node = Element(f"{{{ns}}}{tag}", attrib or {})
    if text is not None:
        node.text = text
    for child in children:
        node.append(child)
    return node


def _package(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _content_types(part: str, content_type: str) -> bytes:
    types = _xml("Types", CT_NS)
    SubElement(
        types,
        f"{{{CT_NS}}}Override",
        {"PartName": "/" + part, "ContentType": content_type},
    )
    return tostring(types, xml_declaration=True, encoding="utf-8")


def _rels(target: str) -> bytes:
    relationships = _xml("Relationships", REL_NS)
    SubElement(
        relationships,
        f"{{{REL_NS}}}Relationship",
        {
            "Id": "rId1",
            "Type": OFFICE_REL,
            "Target": target,
        },
    )
    return tostring(relationships, xml_declaration=True, encoding="utf-8")


def _main(tag: str, ns: str) -> bytes:
    return tostring(_xml(tag, ns), xml_declaration=True, encoding="utf-8")


def intact_docx() -> bytes:
    return _package(
        {
            "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": _main("document", WORD_NS),
        }
    )


def intact_xlsx() -> bytes:
    return _package(
        {
            "[Content_Types].xml": _content_types("xl/workbook.xml", SHEET_TYPE),
            "_rels/.rels": _rels("xl/workbook.xml"),
            "xl/workbook.xml": _main("workbook", SHEET_NS),
        }
    )


def intact_pptx() -> bytes:
    return _package(
        {
            "[Content_Types].xml": _content_types("ppt/presentation.xml", SLIDE_TYPE),
            "_rels/.rels": _rels("ppt/presentation.xml"),
            "ppt/presentation.xml": _main("presentation", SLIDE_NS),
        }
    )


def test_intact_three_formats_are_accepted():
    ooxml.validate_ooxml(intact_docx(), "docx")
    ooxml.validate_ooxml(intact_xlsx(), "xlsx")
    ooxml.validate_ooxml(intact_pptx(), "pptx")


@pytest.mark.parametrize(
    "body",
    (b"", b"not-a-zip", b"PK\x03\x04truncated"),
)
def test_empty_and_non_zip_bytes_are_corrupt(body):
    with pytest.raises(ooxml.CorruptDocumentError) as error:
        ooxml.validate_ooxml(body, "docx")
    assert error.value.reason == "corrupt_document"


def test_xlsx_container_under_docx_name_is_corrupt():
    with pytest.raises(ooxml.CorruptDocumentError) as error:
        ooxml.validate_ooxml(intact_xlsx(), "docx")
    assert error.value.reason == "corrupt_document"


def test_wrong_main_part_root_is_corrupt():
    body = _package(
        {
            "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": _main("workbook", SHEET_NS),
        }
    )
    with pytest.raises(ooxml.CorruptDocumentError) as error:
        ooxml.validate_ooxml(body, "docx")
    assert error.value.reason == "corrupt_document"


def test_encrypted_flag_and_encrypted_member_are_corrupt():
    encoded = bytearray(intact_docx())
    local = encoded.index(b"PK\x03\x04")
    central = encoded.index(b"PK\x01\x02")
    encoded[local + 6] |= 1
    encoded[central + 8] |= 1
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(bytes(encoded), "docx")
    encrypted = _package(
        {
            "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": _main("document", WORD_NS),
            "EncryptedPackage": b"secret",
        }
    )
    with pytest.raises(ooxml.CorruptDocumentError) as error:
        ooxml.validate_ooxml(encrypted, "docx")
    assert error.value.reason == "corrupt_document"


def test_large_main_and_many_presentation_members_are_accepted():
    members = {
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": (
            f'<w:document xmlns:w="{WORD_NS}"><w:body><w:p><w:r><w:t>'
            + "a" * 70000 + "</w:t></w:r></w:p></w:body></w:document>"
        ).encode(),
    }
    ooxml.validate_ooxml(_package(members), "docx")
    presentation = {
        "[Content_Types].xml": _content_types("ppt/presentation.xml", SLIDE_TYPE),
        "_rels/.rels": _rels("ppt/presentation.xml"),
        "ppt/presentation.xml": _main("presentation", SLIDE_NS),
    }
    for index in range(2000):
        presentation[f"ppt/slides/part{index}.xml"] = b"<a/>"
    ooxml.validate_ooxml(_package(presentation), "pptx")


def test_doctype_entity_and_absolute_member_names_are_corrupt():
    doctype = _package(
        {
            "[Content_Types].xml": b'<?xml version="1.0"?><!DOCTYPE Types [<!ENTITY x "y">]><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": _main("document", WORD_NS),
        }
    )
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(doctype, "docx")
    escaped = _package(
        {
            "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
            "_rels/.rels": _rels("../secret.xml"),
            "word/document.xml": _main("document", WORD_NS),
        }
    )
    with pytest.raises(ooxml.CorruptDocumentError) as error:
        ooxml.validate_ooxml(escaped, "docx")
    assert error.value.reason == "corrupt_document"


def test_utf16_doctype_and_entity_are_corrupt():
    main = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE document [<!ENTITY text "expanded">]>'
        f'<w:document xmlns:w="{WORD_NS}"><w:body><w:p>'
        '<w:r><w:t>&text;</w:t></w:r></w:p></w:body></w:document>'
    ).encode("utf-16")
    package = _package({
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": main,
    })
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(package, "docx")


def test_wrong_override_takes_precedence_over_matching_default():
    types = _xml("Types", CT_NS)
    SubElement(types, f"{{{CT_NS}}}Default", {"Extension": "xml", "ContentType": WORD_TYPE})
    SubElement(types, f"{{{CT_NS}}}Override", {
        "PartName": "/word/document.xml", "ContentType": SHEET_TYPE,
    })
    package = _package({
        "[Content_Types].xml": tostring(types),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": _main("document", WORD_NS),
    })
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(package, "docx")


def test_external_relationship_cannot_select_internal_main_part():
    rels = _xml("Relationships", REL_NS)
    SubElement(rels, f"{{{REL_NS}}}Relationship", {
        "Id": "rId1", "Type": OFFICE_REL, "Target": "word/document.xml", "TargetMode": "External",
    })
    package = _package({
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": tostring(rels),
        "word/document.xml": _main("document", WORD_NS),
    })
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(package, "docx")


def test_truncated_main_and_bad_crc_are_corrupt():
    package = _package({
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": f'<w:document xmlns:w="{WORD_NS}"><w:body>'.encode(),
    })
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(package, "docx")
    encoded = bytearray(intact_docx())
    with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
        info = archive.getinfo("word/document.xml")
    offset = info.header_offset
    name_bytes = int.from_bytes(encoded[offset + 26:offset + 28], "little")
    extra_bytes = int.from_bytes(encoded[offset + 28:offset + 30], "little")
    encoded[offset + 30 + name_bytes + extra_bytes + info.file_size - 1] ^= 1
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(bytes(encoded), "docx")


def test_compressed_main_exceeding_inspected_budget_is_corrupt():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types("word/document.xml", WORD_TYPE))
        archive.writestr("_rels/.rels", _rels("word/document.xml"))
        with archive.open("word/document.xml", "w") as main:
            main.write(f'<w:document xmlns:w="{WORD_NS}"><w:body>'.encode())
            for _ in range(1601):
                main.write(b"x" * 65536)
            main.write(b"</w:body></w:document>")
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(buffer.getvalue(), "docx")
