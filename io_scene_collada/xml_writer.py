# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exact OpenCOLLADA COLLADASW::StreamWriter-compatible XML writer.

Produces byte-for-byte identical formatting to Blender 4.5.1's OpenCOLLADA writer:
- UTF-8 XML declaration with LF line endings and no trailing newline after </COLLADA>
- 2-space indentation
- Self-closing empty elements (<tag .../>)
- Exact %.7g float/double formatting matching OpenCOLLADA's COLLADABU::Utils::toString
"""

from __future__ import annotations

import math
import struct
from typing import Iterable, Sequence


def f32(val: float) -> float:
    """Round a Python float (64-bit) to IEEE 754 32-bit float precision."""
    return struct.unpack("<f", struct.pack("<f", float(val)))[0]


def fmt_float(val: float, precision: int = 7) -> str:
    """Format a float/double matching OpenCOLLADA's COLLADABU::Utils::toString."""
    v = float(val)
    if math.isnan(v) or math.isinf(v):
        return "0"
    if v == 0.0:
        return "0"
    # OpenCOLLADA uses 6 sig figs in scientific notation (< 1e-4) and strips leading 0 in exponent
    if abs(v) < 1e-4 or abs(v) >= 1e7:
        s = f"{v:.6g}"
    else:
        s = f"{v:.{precision}g}"
    if s == "-0":
        return "0"
    if "e-0" in s:
        s = s.replace("e-0", "e-")
    elif "e+0" in s:
        s = s.replace("e+0", "e+")
    return s


def fmt_f32(val: float) -> str:
    """Format a 32-bit float matching OpenCOLLADA float output."""
    return fmt_float(f32(val), 7)


def _fmt_f32_sanitized(v: float) -> str:
    """Fast formatter for already-float32, finite, +0-normalized scalars."""
    if v == 0.0:
        return "0"
    av = -v if v < 0.0 else v
    if av < 1e-4 or av >= 1e7:
        s = f"{v:.6g}"
        if "e-0" in s:
            return s.replace("e-0", "e-")
        if "e+0" in s:
            return s.replace("e+0", "e+")
        return s
    return f"{v:.7g}"


from ._native_loader import native_fmt_f32_array, native_fmt_i32_array


def fmt_f32_array(arr) -> str:
    """Format a 1D/ND numpy float32 array to space-separated OpenCOLLADA floats."""
    res = native_fmt_f32_array(arr)
    if res is not None:
        return res

    import numpy as np

    a = np.asarray(arr, dtype=np.float32).ravel()
    if a.size == 0:
        return ""
    if not np.isfinite(a).all():
        a = np.where(np.isfinite(a), a, np.float32(0.0))
    # Normalize -0.0 to +0.0
    if (a == 0.0).any():
        a = a.copy()
        a[a == 0.0] = 0.0
    return " ".join(map(_fmt_f32_sanitized, a.tolist()))


def fmt_int_array(arr) -> str:
    """Format a 1D/ND numpy integer array to space-separated ints."""
    res = native_fmt_i32_array(arr)
    if res is not None:
        return res
    return " ".join(map(str, arr.ravel().tolist()))


def fmt_int(val: int) -> str:
    return str(int(val))


def fmt_bool(val: bool) -> str:
    return "1" if val else "0"


def encode_xml(text: str) -> str:
    """Exact port of collada_internal.cpp encode_xml()."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def escape_attr(text: str) -> str:
    """Escape XML attribute value (avoid double-escaping if already encoded)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_NL_INDENTS = tuple("\n" + "  " * i for i in range(64))


class ColladaStreamWriter:
    """Streaming XML writer matching OpenCOLLADA's COLLADASW::StreamWriter."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._stack: list[str] = []
        self._open_tag_unclosed: bool = False
        self._has_text: bool = False
        self._has_children: bool = False

    def _indent(self, level: int | None = None) -> str:
        n = len(self._stack) if level is None else level
        return "  " * n

    def _finish_open_tag(self) -> None:
        if self._open_tag_unclosed:
            self._chunks.append(">")
            self._open_tag_unclosed = False

    def start_document(self) -> None:
        self._chunks.append('<?xml version="1.0" encoding="utf-8"?>\n')
        self._chunks.append(
            '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" '
            'version="1.4.1" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        )
        self._stack.append("COLLADA")
        self._open_tag_unclosed = False
        self._has_text = False
        self._has_children = True

    def end_document(self) -> None:
        self._finish_open_tag()
        if self._stack and self._stack[-1] == "COLLADA":
            self._stack.pop()
        self._chunks.append("\n</COLLADA>")

    def open_element(self, name: str, attrs: Sequence[tuple[str, str]] | None = None) -> None:
        chunks = self._chunks
        if self._open_tag_unclosed:
            chunks.append(">")
        depth = len(self._stack)
        prefix = _NL_INDENTS[depth] if depth < 64 else ("\n" + "  " * depth)
        if attrs:
            chunks.append(f"{prefix}<{name}" + "".join(f' {k}="{v}"' for k, v in attrs))
        else:
            chunks.append(f"{prefix}<{name}")
        self._stack.append(name)
        self._open_tag_unclosed = True
        self._has_text = False
        self._has_children = False

    def close_element(self) -> None:
        name = self._stack.pop()
        chunks = self._chunks
        if self._open_tag_unclosed:
            chunks.append("/>")
            self._open_tag_unclosed = False
        elif self._has_text:
            chunks.append(f"</{name}>")
        else:
            depth = len(self._stack)
            prefix = _NL_INDENTS[depth] if depth < 64 else ("\n" + "  " * depth)
            chunks.append(f"{prefix}</{name}>")
        self._has_text = False
        self._has_children = True

    def append_text(self, text: str) -> None:
        self._finish_open_tag()
        self._chunks.append(text)
        self._has_text = True

    def append_values(self, values: Iterable[str]) -> None:
        self._finish_open_tag()
        joined = " ".join(values)
        if joined:
            self._chunks.append(joined)
        self._has_text = True

    def write_element(
        self,
        name: str,
        text: str | None = None,
        attrs: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        self.open_element(name, attrs)
        if text is not None:
            self.append_text(text)
        self.close_element()

    def write_extra_technique(
        self,
        profile: str,
        params: Sequence[tuple[str, str | None, str | None, str]],
    ) -> None:
        """Write <extra><technique profile="..."><tag sid="..." type="...">val</tag>...</technique></extra>.

        Each item in params is (tag_name, sid_or_none, type_or_none, text_value).
        """
        if not params:
            return
        self.open_element("extra")
        self.open_element("technique", [("profile", profile)])
        for tag_name, sid, ptype, text_val in params:
            attrs: list[tuple[str, str]] = []
            if sid is not None and sid != "":
                attrs.append(("sid", sid))
            if ptype is not None and ptype != "":
                attrs.append(("type", ptype))
            self.write_element(tag_name, text_val, attrs if attrs else None)
        self.close_element()
        self.close_element()

    def get_bytes(self) -> bytes:
        return "".join(self._chunks).encode("utf-8")

    def write_to_file(self, filepath: str) -> None:
        with open(filepath, "wb") as f:
            f.write(self.get_bytes())
