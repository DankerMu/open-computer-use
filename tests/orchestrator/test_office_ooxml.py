# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for bounded OOXML package validation."""
from __future__ import annotations

import io
import struct
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


def _package(members: dict[str, bytes], compress_type=zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = compress_type
            archive.writestr(info, body)
    return buffer.getvalue()


def _member_utf8_name(extra_ascii: str = "") -> str:
    # Complete ZIP member name, including directory prefix and suffix.
    # "word/" (5) + 507 * "é" (1014) + "x" (1) + extra + ".xml" (4) => 1024 + len(extra).
    return "word/" + ("é" * 507) + "x" + extra_ascii + ".xml"


def _central_entry_offset(archive, encoded: bytes, name: str) -> int:
    encoded_name = name.encode("utf-8")
    cursor = archive.start_dir
    while True:
        header = encoded[cursor:cursor + 46]
        assert header[:4] == b"PK\x01\x02"
        name_len = int.from_bytes(header[28:30], "little")
        extra_len = int.from_bytes(header[30:32], "little")
        comment_len = int.from_bytes(header[32:34], "little")
        if encoded[cursor + 46:cursor + 46 + name_len] == encoded_name:
            return cursor
        cursor += 46 + name_len + extra_len + comment_len


def _forge_uncompressed_size(encoded: bytearray, name: str, size: int = 8) -> bytes:
    with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
        info = archive.getinfo(name)
        extra = int.from_bytes(encoded[info.header_offset + 28:info.header_offset + 30], "little")
        name_len = int.from_bytes(encoded[info.header_offset + 26:info.header_offset + 28], "little")
        assert encoded[info.header_offset:info.header_offset + 4] == b"PK\x03\x04"
        assert extra == 0
        assert encoded[info.header_offset + 30:info.header_offset + 30 + name_len] == name.encode("utf-8")
        encoded[info.header_offset + 22:info.header_offset + 26] = struct.pack("<I", size)
        central = _central_entry_offset(archive, encoded, name)
        encoded[central + 24:central + 28] = struct.pack("<I", size)
    return bytes(encoded)


def _unsupported_codec_package(codec, member: str, forge: bool) -> bytes:
    comment = "<!--" + ("x" * (1024 * 1024)) + "-->"
    members = {
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": _main("document", WORD_NS),
    }
    if member == "[Content_Types].xml":
        members[member] = _content_types("word/document.xml", WORD_TYPE).replace(
            b"?>", b"?>" + comment.encode("ascii"), 1
        )
    elif member == "_rels/.rels":
        members[member] = _rels("word/document.xml").replace(
            b"?>", b"?>" + comment.encode("ascii"), 1
        )
    else:
        members[member] = _main("document", WORD_NS).replace(
            b"?>", b"?>" + comment.encode("ascii"), 1
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = codec if name == member else zipfile.ZIP_STORED
            archive.writestr(info, body)
    encoded = bytearray(buffer.getvalue())
    if forge:
        return _forge_uncompressed_size(encoded, member)
    return bytes(encoded)



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


def many_member_pptx() -> bytes:
    presentation = {
        "[Content_Types].xml": _content_types("ppt/presentation.xml", SLIDE_TYPE),
        "_rels/.rels": _rels("ppt/presentation.xml"),
        "ppt/presentation.xml": _main("presentation", SLIDE_NS),
    }
    for index in range(2000):
        presentation[f"ppt/slides/part{index}.xml"] = b"<a/>"
    return _package(presentation)


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


def test_oversized_utf8_member_name_is_corrupt():
    accepted = _member_utf8_name()
    rejected = _member_utf8_name("x")
    assert len(accepted.encode("utf-8")) == 1024
    assert len(rejected.encode("utf-8")) == 1025
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(
            _package(
                {
                    "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
                    "_rels/.rels": _rels("word/document.xml"),
                    "word/document.xml": _main("document", WORD_NS),
                    rejected: b"<a/>",
                }
            ),
            "docx",
        )


def test_aggregate_inspected_budget_rejects_when_parts_fit_individually(monkeypatch):
    members = {
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": _main("document", WORD_NS),
    }
    lengths = [len(body) for body in members.values()]
    budget = max(lengths)
    assert all(length <= budget for length in lengths)
    assert sum(lengths) > budget
    monkeypatch.setattr(ooxml, "MAX_INSPECTED_BYTES", budget)
    with pytest.raises(ooxml.CorruptDocumentError):
        ooxml.validate_ooxml(_package(members, compress_type=zipfile.ZIP_DEFLATED), "docx")



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


