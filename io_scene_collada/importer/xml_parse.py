# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""XML safety, namespace traversal, strict array parsing, and URI/path helpers."""

from __future__ import annotations

import io
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
import xml.parsers.expat as _expat

import numpy as np

from .._native_loader import native_parse_f32, native_parse_i32

_DTD_CHUNK_BYTES = 65536


class _XmlPrologDone(Exception):
    pass


def _reject_dtd_expat(source: str | bytes) -> None:
    """Reject DOCTYPE / ENTITY / NOTATION declarations at the expat parser level.

    Feeds only the XML prolog (stopping as soon as the root element starts) so
    multi-megabyte COLLADA files incur sub-millisecond overhead while arbitrary-length
    leading comments, processing instructions, and encodings are handled accurately.
    """
    parser = _expat.ParserCreate()

    def _forbid(*_args, **_kwargs) -> None:
        raise ValueError("DTDs are not supported in COLLADA files")

    def _on_root(*_args, **_kwargs) -> None:
        raise _XmlPrologDone()

    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.UnparsedEntityDeclHandler = _forbid
    parser.NotationDeclHandler = _forbid
    parser.StartElementHandler = _on_root

    try:
        if isinstance(source, (bytes, bytearray)):
            mv = memoryview(source)
            n = len(mv)
            for off in range(0, n, _DTD_CHUNK_BYTES):
                end = min(off + _DTD_CHUNK_BYTES, n)
                parser.Parse(mv[off:end], end == n)
            if n == 0:
                parser.Parse(b"", True)
        else:
            with open(source, "rb") as fh:
                while True:
                    chunk = fh.read(_DTD_CHUNK_BYTES)
                    if not chunk:
                        parser.Parse(b"", True)
                        break
                    parser.Parse(chunk, False)
    except _XmlPrologDone:
        return
    except _expat.ExpatError:
        # Let ET.parse report ordinary XML syntax errors uniformly.
        return


def _parse_xml_safe(source: str | bytes) -> ET.ElementTree:
    """Parse XML from a file path or bytes after rejecting DTD declarations via expat."""
    _reject_dtd_expat(source)
    if isinstance(source, (bytes, bytearray)):
        return ET.parse(io.BytesIO(source))
    return ET.parse(source)



def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for ch in elem:
        if _strip_ns(ch.tag) == name:
            return ch
    return None


def _find_children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [ch for ch in elem if _strip_ns(ch.tag) == name]


_LEGACY_NONFINITE_RE = re.compile(r"[+-]?\d+\.#(?:QNAN|SNAN|IND|INF)\d*", re.IGNORECASE)


def _fromstring_f32_strict(text: str):
    """Parse whitespace-separated float32 values without relying on NumPy DeprecationWarning."""
    import numpy as np

    toks = text.split()
    if not toks:
        return np.empty(0, dtype=np.float32)
    # Validate last token explicitly: NumPy 1.26's fromstring stops mid-token on trailing
    # garbage (e.g. "1.0 2.0 3.0x" or "1.0 2.0 1.#QNAN") while still returning len(toks) items.
    float(toks[-1])
    arr = np.fromstring(text, sep=" ", dtype=np.float32)
    if len(arr) != len(toks):
        raise ValueError("Unmatched data in float array")
    return arr


def _fromstring_i32_strict(text: str):
    """Parse whitespace-separated int32 values without relying on NumPy DeprecationWarning."""
    import numpy as np

    toks = text.split()
    if not toks:
        return np.empty(0, dtype=np.int32)
    int(toks[-1])
    arr = np.fromstring(text, sep=" ", dtype=np.int32)
    if len(arr) != len(toks):
        raise ValueError("Unmatched data in integer array")
    return arr


def _parse_f32_np(text: str | None, warnings: list[str] | None = None, label: str = ""):
    import numpy as np

    if not text or not text.strip():
        return np.empty(0, dtype=np.float32)
    res = native_parse_f32(text)
    if res is not None:
        return res
    try:
        return _fromstring_f32_strict(text)
    except ValueError:
        if "#" in text:
            cleaned, count = _LEGACY_NONFINITE_RE.subn("0.0", text)
            if count > 0:
                try:
                    arr = _fromstring_f32_strict(cleaned)
                    if warnings is not None:
                        warnings.append(f"Replaced {count} legacy non-finite token(s) with 0.0 in {label}")
                    return arr
                except ValueError:
                    pass
        if warnings is not None:
            warnings.append(f"Invalid number in {label}")
        return None


def _parse_i32_np(text: str | None, warnings: list[str] | None = None, label: str = ""):
    import numpy as np

    if not text or not text.strip():
        return np.empty(0, dtype=np.int32)
    res = native_parse_i32(text)
    if res is not None:
        return res
    try:
        return _fromstring_i32_strict(text)
    except ValueError:
        if warnings is not None:
            warnings.append(f"Invalid integer in {label}")
        return None


def _safe_float(text: str | None, default: float = 0.0) -> float:
    if not text:
        return default
    try:
        v = float(text.strip())
        return v if math.isfinite(v) else default
    except ValueError:
        return default


def _safe_int(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    try:
        return int(text.strip())
    except ValueError:
        return default


def _parse_floats(text: str | None) -> list[float]:
    if not text:
        return []
    toks = text.split()
    try:
        vals = [float(x) for x in toks]
    except ValueError:
        vals = []
        for x in toks:
            try:
                vals.append(float(x))
            except ValueError:
                vals.append(0.0)
    return [v if math.isfinite(v) else 0.0 for v in vals]


def _parse_ints(text: str | None) -> list[int]:
    if not text:
        return []
    toks = text.split()
    try:
        return [int(x) for x in toks]
    except ValueError:
        out: list[int] = []
        for x in toks:
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out


def _parse_strings(text: str | None) -> list[str]:
    if not text:
        return []
    return text.split()


def _slice_accessor_array(vals, acc: ET.Element | None, stride: int, warnings: list[str], label: str):
    """Apply COLLADA <accessor> offset and count to a float/string source array."""
    if acc is None:
        return vals
    offset = 0
    if "offset" in acc.attrib:
        try:
            offset = int(acc.attrib["offset"])
        except ValueError:
            warnings.append(f"Invalid accessor offset '{acc.attrib['offset']}' in {label}; using 0")
            offset = 0
        if offset < 0:
            warnings.append(f"Negative accessor offset {offset} in {label}; clamping to 0")
            offset = 0

    count: int | None = None
    if "count" in acc.attrib:
        try:
            count = int(acc.attrib["count"])
        except ValueError:
            warnings.append(f"Invalid accessor count '{acc.attrib['count']}' in {label}")
            count = None
        else:
            if count < 0:
                warnings.append(f"Negative accessor count {count} in {label}; clamping to 0")
                count = 0

    if offset == 0 and count is None:
        return vals

    n = len(vals)
    if offset > n:
        warnings.append(f"Accessor offset {offset} exceeds array length {n} in {label}; clamped")
        return vals[:0]
    if count is None:
        return vals[offset:]
    step = stride if stride > 0 else 1
    end = offset + count * step
    if end > n:
        warnings.append(f"Accessor range [{offset}:{end}] exceeds array length {n} in {label}; clamped")
        end = n
    return vals[offset:end]


def _parse_extra_blender(elem: ET.Element) -> dict[str, str]:
    """Extract all <extra><technique profile="blender"> child elements as tag->text."""
    result: dict[str, str] = {}
    for extra in _find_children(elem, "extra"):
        for tech in _find_children(extra, "technique"):
            if tech.attrib.get("profile") == "blender":
                for ch in tech:
                    result[_strip_ns(ch.tag)] = ch.text or ""
    return result


def _uri_to_local_id(uri: str | None) -> str:
    if not uri:
        return ""
    if uri.startswith("#"):
        return uri[1:]
    return uri


def _uri_to_native_path(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file:///"):
        p = urllib.parse.unquote(uri[8:])
        if len(p) >= 2 and p[1] == ":":
            return p.replace("/", os.sep)
        return "/" + p
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[7:])
    return urllib.parse.unquote(uri)


def _norm_archive_path(raw: str) -> str:
    """Normalize a zip entry or URI path strictly within the archive namespace."""
    s = raw.replace("\\", "/")
    parts: list[str] = []
    for seg in s.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)

