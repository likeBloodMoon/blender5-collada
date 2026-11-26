# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Single lenient JSON / jbeam parser used for .jbeam, .pc, info*.json, and main.materials.json.

Handles relaxed JSON features common in BeamNG files:
  - UTF-8 BOM
  - // line comments, /* */ block comments, and # shell-style comments
  - Trailing commas and omitted commas (same line or across lines)
  - Numbers such as 1., .5, +1, scientific notation
  - Bare words: true, false, null, FLT_MAX, -FLT_MAX, nan, NaN, inf, -inf, Infinity, -Infinity
  - Unquoted object keys and ':' or '=' key-value separators
  - Standard JSON escapes including \\uXXXX
  - Duplicate keys: last key wins (matching json.loads)

Raises JbeamError(msg, line, col) with 1-based line and column on syntax errors.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = ["JbeamError", "parse"]


class JbeamError(ValueError):
    """Raised when lenient JSON / jbeam input has a real syntax error."""

    def __init__(self, msg: str, line: int, col: int) -> None:
        super().__init__(f"{msg} at line {line}, col {col}")
        self.msg = msg
        self.line = line
        self.col = col


_NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_INT_RE = re.compile(r"[+-]?\d+")
_WORD_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.\-]*")

_BARE_VALUES: dict[str, Any] = {
    "true": True,
    "false": False,
    "null": None,
    "FLT_MAX": 3.4028234663852886e38,
    "-FLT_MAX": -3.4028234663852886e38,
    "nan": math.nan,
    "NaN": math.nan,
    "inf": math.inf,
    "-inf": -math.inf,
    "Infinity": math.inf,
    "-Infinity": -math.inf,
}

_ESCAPES: dict[str, str] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


MAX_NESTING_DEPTH = 256


class _Parser:
    __slots__ = ("s", "i", "n", "depth")

    def __init__(self, text: str) -> None:
        if text.startswith("\ufeff"):
            text = text[1:]
        self.s = text
        self.i = 0
        self.n = len(text)
        self.depth = 0

    def _error(self, msg: str, pos: int | None = None) -> None:
        p = self.i if pos is None else pos
        line = self.s.count("\n", 0, p) + 1
        col = p - (self.s.rfind("\n", 0, p) + 1) + 1
        raise JbeamError(msg, line, col)

    def _skip_ws_and_commas(self) -> None:
        s = self.s
        n = self.n
        i = self.i
        while i < n:
            c = s[i]
            if c in " \t\r\n,":
                i += 1
            elif c == "/" and i + 1 < n and s[i + 1] == "/":
                j = s.find("\n", i + 2)
                i = n if j < 0 else j + 1
            elif c == "/" and i + 1 < n and s[i + 1] == "*":
                j = s.find("*/", i + 2)
                if j < 0:
                    self._error("unterminated block comment", i)
                i = j + 2
            elif c == "#":
                j = s.find("\n", i + 1)
                i = n if j < 0 else j + 1
            else:
                break
        self.i = i

    def _parse_string(self) -> str:
        s = self.s
        n = self.n
        start_quote = self.i
        i = start_quote + 1
        out: list[str] = []
        chunk_start = i
        while i < n:
            c = s[i]
            if c == '"':
                out.append(s[chunk_start:i])
                self.i = i + 1
                return "".join(out)
            if c == "\\":
                if i + 1 >= n:
                    self._error("unterminated string escape", i)
                out.append(s[chunk_start:i])
                esc = s[i + 1]
                if esc == "u" and i + 5 < n:
                    hex_part = s[i + 2 : i + 6]
                    try:
                        out.append(chr(int(hex_part, 16)))
                        i += 6
                    except ValueError:
                        out.append("\\u")
                        i += 2
                else:
                    out.append(_ESCAPES.get(esc, esc))
                    i += 2
                chunk_start = i
                continue
            i += 1
        self._error("unterminated string", start_quote)
        return ""  # unreachable

    def parse_value(self) -> Any:
        self._skip_ws_and_commas()
        if self.i >= self.n:
            self._error("unexpected end of file")
        c = self.s[self.i]
        if c == "{":
            return self._parse_object()
        if c == "[":
            return self._parse_array()
        if c == '"':
            return self._parse_string()

        # Special negative bare constants before numeric check
        for neg_tok in ("-FLT_MAX", "-Infinity", "-inf"):
            if self.s.startswith(neg_tok, self.i):
                end_pos = self.i + len(neg_tok)
                if end_pos >= self.n or not (self.s[end_pos].isalnum() or self.s[end_pos] == "_"):
                    self.i = end_pos
                    return _BARE_VALUES[neg_tok]

        m = _NUM_RE.match(self.s, self.i)
        if m and m.end() > self.i:
            tok = m.group(0)
            self.i = m.end()
            if _INT_RE.fullmatch(tok):
                return int(tok)
            try:
                return float(tok)
            except ValueError:
                return float(tok.rstrip("."))

        m = _WORD_RE.match(self.s, self.i)
        if m:
            word = m.group(0)
            self.i = m.end()
            return _BARE_VALUES.get(word, word)

        self._error(f"unexpected character {c!r}")
        return None  # unreachable

    def _parse_object(self) -> dict[str, Any]:
        open_pos = self.i
        self.depth += 1
        if self.depth > MAX_NESTING_DEPTH:
            self._error(f"maximum nesting depth exceeded ({MAX_NESTING_DEPTH})", open_pos)
        self.i += 1
        obj: dict[str, Any] = {}
        try:
            while True:
                self._skip_ws_and_commas()
                if self.i >= self.n:
                    self._error("unterminated object", open_pos)
                c = self.s[self.i]
                if c == "}":
                    self.i += 1
                    return obj
                if c == '"':
                    key = self._parse_string()
                else:
                    m = _WORD_RE.match(self.s, self.i) or _NUM_RE.match(self.s, self.i)
                    if not m:
                        self._error(f"invalid object key {c!r}")
                    key = m.group(0)
                    self.i = m.end()
                self._skip_ws_and_commas()
                if self.i < self.n and self.s[self.i] in ":=":
                    self.i += 1
                else:
                    self._error(f"missing ':' after key {key!r}")
                obj[key] = self.parse_value()
        finally:
            self.depth -= 1

    def _parse_array(self) -> list[Any]:
        open_pos = self.i
        self.depth += 1
        if self.depth > MAX_NESTING_DEPTH:
            self._error(f"maximum nesting depth exceeded ({MAX_NESTING_DEPTH})", open_pos)
        self.i += 1
        arr: list[Any] = []
        try:
            while True:
                self._skip_ws_and_commas()
                if self.i >= self.n:
                    self._error("unterminated array", open_pos)
                if self.s[self.i] == "]":
                    self.i += 1
                    return arr
                arr.append(self.parse_value())
        finally:
            self.depth -= 1


def parse(text: str) -> Any:
    """Parse relaxed JSON / jbeam text into Python values."""
    parser = _Parser(text)
    val = parser.parse_value()
    parser._skip_ws_and_commas()
    if parser.i < parser.n:
        parser._error("trailing data after top-level value")
    return val
