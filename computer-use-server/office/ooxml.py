# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Stream inspected OOXML parts without extraction or full schema validation."""
from __future__ import annotations

import io
import zipfile
import zlib
from xml.parsers import expat

from outputs_broker import MAX_FILE_SIZE

CONTENT_TYPES_NAME = "[Content_Types].xml"
PACKAGE_RELS_NAME = "_rels/.rels"
MAX_MEMBER_NAME_BYTES = 1024
MAX_INSPECTED_BYTES = MAX_FILE_SIZE
_READ_CHUNK = 64 * 1024
_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_FORMATS = {
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main document",
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main workbook",
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "http://schemas.openxmlformats.org/presentationml/2006/main presentation",
    ),
}


class CorruptDocumentError(RuntimeError):
    """Bytes are not a readable OOXML package of the declared type."""

    reason = "corrupt_document"

    def __init__(self) -> None:
        super().__init__("document is not a readable OOXML package")


def validate_ooxml(content: bytes, document_type: str) -> None:
    spec = _FORMATS.get(document_type)
    if spec is None or not isinstance(content, (bytes, bytearray)) or len(content) > MAX_FILE_SIZE:
        raise CorruptDocumentError()
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
            seen = set()
            for info in archive.infolist():
                name = info.filename
                if (
                    not _safe_member_name(info.orig_filename)
                    or name in seen
                    or info.flag_bits & 1
                ):
                    raise CorruptDocumentError()
                if info.compress_type not in _ALLOWED_COMPRESSION:
                    raise CorruptDocumentError()
                seen.add(name)
                if name.rstrip("/").rsplit("/", 1)[-1].lower() in {"encryptioninfo", "encryptedpackage"}:
                    raise CorruptDocumentError()
            if CONTENT_TYPES_NAME not in seen or PACKAGE_RELS_NAME not in seen:
                raise CorruptDocumentError()
            budget = [MAX_INSPECTED_BYTES]
            main_part = _main_part(archive, budget)
            if main_part not in seen or main_part.endswith("/"):
                raise CorruptDocumentError()
            _check_content_type(archive, budget, main_part, spec[0])
            _parse_member(archive, main_part, budget, spec[1])
    except CorruptDocumentError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError,
            NotImplementedError, zlib.error, expat.ExpatError, UnicodeError, ValueError):
        raise CorruptDocumentError() from None


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    if len(name.encode("utf-8")) > MAX_MEMBER_NAME_BYTES:
        return False
    parts = name.rstrip("/").split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _parse_member(archive, name, budget, root_name, visit=None) -> None:
    parser = expat.ParserCreate(namespace_separator=" ")
    depth = 0

    def start(element, attributes):
        nonlocal depth
        depth += 1
        if depth == 1 and element != root_name:
            raise CorruptDocumentError()
        if visit is not None and depth == 2:
            visit(element, attributes)

    def end(_element):
        nonlocal depth
        depth -= 1

    def reject(*_args):
        raise CorruptDocumentError()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    # No text handler or tree: large main parts and metadata do not accumulate.
    with archive.open(name) as member:
        while True:
            chunk = member.read(min(_READ_CHUNK, budget[0] + 1))
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise CorruptDocumentError()
            parser.Parse(chunk, False)
        parser.Parse(b"", True)


def _main_part(archive, budget) -> str:
    target = None

    def visit(element, attributes):
        nonlocal target
        if element != f"{_NS_REL} Relationship" or attributes.get("Type") != _OFFICE_REL:
            return
        if attributes.get("TargetMode", "Internal") != "Internal":
            return
        candidate = attributes.get("Target", "").removeprefix("/")
        if not _safe_member_name(candidate) or candidate.endswith("/") or target is not None:
            raise CorruptDocumentError()
        target = candidate

    _parse_member(archive, PACKAGE_RELS_NAME, budget, f"{_NS_REL} Relationships", visit)
    if target is None:
        raise CorruptDocumentError()
    return target


def _check_content_type(archive, budget, main_part, expected) -> None:
    override = None
    default = None
    extension = main_part.rsplit(".", 1)[-1].lower()

    def visit(element, attributes):
        nonlocal override, default
        if element == f"{_NS_CT} Override" and attributes.get("PartName") == "/" + main_part:
            if override is not None:
                raise CorruptDocumentError()
            override = attributes.get("ContentType", "")
        elif element == f"{_NS_CT} Default" and attributes.get("Extension", "").lower() == extension:
            if default is not None:
                raise CorruptDocumentError()
            default = attributes.get("ContentType", "")

    _parse_member(archive, CONTENT_TYPES_NAME, budget, f"{_NS_CT} Types", visit)
    selected = override if override is not None else default
    if selected != expected:
        raise CorruptDocumentError()
