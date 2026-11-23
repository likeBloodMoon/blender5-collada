# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Minimal, bounds-checked pure-Python MsgPack reader/writer for BeamNG .cdae v31.

Covers the MsgPack types used by .cdae v31 (ints, floats, bool, nil, str, bin,
array, map). Binary blocks are returned as zero-copy memoryview slices so callers
can pass them directly to numpy.frombuffer without per-element Python loops.
"""

from __future__ import annotations

import struct
from typing import Any


class MsgPackError(ValueError):
    """Raised when a MsgPack stream is malformed, truncated, or exceeds bounds."""


_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")
_I8 = struct.Struct(">b")
_I16 = struct.Struct(">h")
_I32 = struct.Struct(">i")
_I64 = struct.Struct(">q")
_F32 = struct.Struct(">f")
_F64 = struct.Struct(">d")

# Depth guard against maliciously nested arrays/maps
_MAX_DEPTH = 32


class MsgPackReader:
    """Sequential MsgPack stream reader with strict remaining-buffer bounds checks."""

    __slots__ = ("_mv", "_len", "_pos")

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self._mv = memoryview(data)
        self._len = len(self._mv)
        self._pos = 0

    @property
    def pos(self) -> int:
        return self._pos

    @property
    def remaining(self) -> int:
        return self._len - self._pos

    @property
    def eof(self) -> bool:
        return self._pos >= self._len

    def _need(self, n: int) -> int:
        if n < 0 or self._pos + n > self._len:
            raise MsgPackError(
                f"Unexpected end of MsgPack stream at offset {self._pos} "
                f"(needed {n} bytes, {self._len - self._pos} remaining)"
            )
        start = self._pos
        self._pos += n
        return start

    def read(self, *, zero_copy_bin: bool = True, _depth: int = 0) -> Any:
        if _depth > _MAX_DEPTH:
            raise MsgPackError("MsgPack nesting depth limit exceeded")
        b = self._mv[self._need(1)]

        # Positive fixint (0x00 - 0x7f)
        if b <= 0x7F:
            return b
        # Negative fixint (0xe0 - 0xff)
        if b >= 0xE0:
            return b - 0x100
        # Fixstr (0xa0 - 0xbf)
        if 0xA0 <= b <= 0xBF:
            n = b & 0x1F
            s = self._need(n)
            try:
                return bytes(self._mv[s : s + n]).decode("utf-8", errors="replace")
            except Exception as e:
                raise MsgPackError(f"Invalid UTF-8 string: {e}") from e
        # Fixarray (0x90 - 0x9f)
        if 0x90 <= b <= 0x9F:
            return self._read_array(b & 0x0F, zero_copy_bin, _depth + 1)
        # Fixmap (0x80 - 0x8f)
        if 0x80 <= b <= 0x8F:
            return self._read_map(b & 0x0F, zero_copy_bin, _depth + 1)

        if b == 0xC0:
            return None
        if b == 0xC2:
            return False
        if b == 0xC3:
            return True

        # Bin family
        if b == 0xC4:
            n = self._mv[self._need(1)]
            return self._read_bin(n, zero_copy_bin)
        if b == 0xC5:
            n = _U16.unpack_from(self._mv, self._need(2))[0]
            return self._read_bin(n, zero_copy_bin)
        if b == 0xC6:
            n = _U32.unpack_from(self._mv, self._need(4))[0]
            return self._read_bin(n, zero_copy_bin)

        # Floats
        if b == 0xCA:
            return _F32.unpack_from(self._mv, self._need(4))[0]
        if b == 0xCB:
            return _F64.unpack_from(self._mv, self._need(8))[0]

        # Unsigned ints
        if b == 0xCC:
            return self._mv[self._need(1)]
        if b == 0xCD:
            return _U16.unpack_from(self._mv, self._need(2))[0]
        if b == 0xCE:
            return _U32.unpack_from(self._mv, self._need(4))[0]
        if b == 0xCF:
            return _U64.unpack_from(self._mv, self._need(8))[0]

        # Signed ints
        if b == 0xD0:
            return _I8.unpack_from(self._mv, self._need(1))[0]
        if b == 0xD1:
            return _I16.unpack_from(self._mv, self._need(2))[0]
        if b == 0xD2:
            return _I32.unpack_from(self._mv, self._need(4))[0]
        if b == 0xD3:
            return _I64.unpack_from(self._mv, self._need(8))[0]

        # Str family
        if b == 0xD9:
            n = self._mv[self._need(1)]
            s = self._need(n)
            return bytes(self._mv[s : s + n]).decode("utf-8", errors="replace")
        if b == 0xDA:
            n = _U16.unpack_from(self._mv, self._need(2))[0]
            s = self._need(n)
            return bytes(self._mv[s : s + n]).decode("utf-8", errors="replace")
        if b == 0xDB:
            n = _U32.unpack_from(self._mv, self._need(4))[0]
            s = self._need(n)
            return bytes(self._mv[s : s + n]).decode("utf-8", errors="replace")

        # Array family
        if b == 0xDC:
            n = _U16.unpack_from(self._mv, self._need(2))[0]
            return self._read_array(n, zero_copy_bin, _depth + 1)
        if b == 0xDD:
            n = _U32.unpack_from(self._mv, self._need(4))[0]
            return self._read_array(n, zero_copy_bin, _depth + 1)

        # Map family
        if b == 0xDE:
            n = _U16.unpack_from(self._mv, self._need(2))[0]
            return self._read_map(n, zero_copy_bin, _depth + 1)
        if b == 0xDF:
            n = _U32.unpack_from(self._mv, self._need(4))[0]
            return self._read_map(n, zero_copy_bin, _depth + 1)

        raise MsgPackError(f"Unsupported MsgPack type byte 0x{b:02X} at offset {self._pos - 1}")

    def _read_bin(self, n: int, zero_copy: bool) -> memoryview | bytes:
        s = self._need(n)
        sl = self._mv[s : s + n]
        return sl if zero_copy else bytes(sl)

    def _read_array(self, n: int, zero_copy_bin: bool, depth: int) -> list[Any]:
        # Each MsgPack element takes at least 1 byte; reject impossible counts before allocating
        if n > self.remaining:
            raise MsgPackError(
                f"MsgPack array length {n} exceeds remaining stream bytes ({self.remaining})"
            )
        return [self.read(zero_copy_bin=zero_copy_bin, _depth=depth) for _ in range(n)]

    def _read_map(self, n: int, zero_copy_bin: bool, depth: int) -> dict[Any, Any]:
        # Each map entry takes at least 2 bytes (key + value)
        if n * 2 > self.remaining:
            raise MsgPackError(
                f"MsgPack map length {n} exceeds remaining stream bytes ({self.remaining})"
            )
        out: dict[Any, Any] = {}
        for _ in range(n):
            k = self.read(zero_copy_bin=False, _depth=depth)
            v = self.read(zero_copy_bin=zero_copy_bin, _depth=depth)
            out[k] = v
        return out


class MsgPackWriter:
    """Minimal MsgPack stream writer."""

    __slots__ = ("_chunks",)

    def __init__(self) -> None:
        self._chunks: list[bytes | bytearray | memoryview] = []

    def getvalue(self) -> bytes:
        return b"".join(self._chunks)

    def write_nil(self) -> None:
        self._chunks.append(b"\xc0")

    def write_bool(self, val: bool) -> None:
        self._chunks.append(b"\xc3" if val else b"\xc2")

    def write_int(self, val: int) -> None:
        if 0 <= val <= 0x7F:
            self._chunks.append(bytes((val,)))
        elif -32 <= val < 0:
            self._chunks.append(bytes((val & 0xFF,)))
        elif 0 <= val <= 0xFF:
            self._chunks.append(bytes((0xCC, val)))
        elif 0 <= val <= 0xFFFF:
            self._chunks.append(b"\xcd" + _U16.pack(val))
        elif 0 <= val <= 0xFFFFFFFF:
            self._chunks.append(b"\xce" + _U32.pack(val))
        elif 0 <= val <= 0xFFFFFFFFFFFFFFFF:
            self._chunks.append(b"\xcf" + _U64.pack(val))
        elif -128 <= val < 0:
            self._chunks.append(b"\xd0" + _I8.pack(val))
        elif -32768 <= val < 0:
            self._chunks.append(b"\xd1" + _I16.pack(val))
        elif -2147483648 <= val < 0:
            self._chunks.append(b"\xd2" + _I32.pack(val))
        elif -9223372036854775808 <= val < 0:
            self._chunks.append(b"\xd3" + _I64.pack(val))
        else:
            raise MsgPackError(f"Integer out of 64-bit MsgPack range: {val}")

    def write_float32(self, val: float) -> None:
        self._chunks.append(b"\xca" + _F32.pack(float(val)))

    def write_float64(self, val: float) -> None:
        self._chunks.append(b"\xcb" + _F64.pack(float(val)))

    def write_str(self, val: str) -> None:
        raw = val.encode("utf-8")
        n = len(raw)
        if n <= 31:
            self._chunks.append(bytes((0xA0 | n,)))
        elif n <= 0xFF:
            self._chunks.append(bytes((0xD9, n)))
        elif n <= 0xFFFF:
            self._chunks.append(b"\xda" + _U16.pack(n))
        elif n <= 0xFFFFFFFF:
            self._chunks.append(b"\xdb" + _U32.pack(n))
        else:
            raise MsgPackError(f"String too large for MsgPack: {n} bytes")
        if n:
            self._chunks.append(raw)

    def write_bin(self, data: bytes | bytearray | memoryview) -> None:
        n = len(data)
        if n <= 0xFF:
            self._chunks.append(bytes((0xC4, n)))
        elif n <= 0xFFFF:
            self._chunks.append(b"\xc5" + _U16.pack(n))
        elif n <= 0xFFFFFFFF:
            self._chunks.append(b"\xc6" + _U32.pack(n))
        else:
            raise MsgPackError(f"Binary block too large for MsgPack: {n} bytes")
        if n:
            self._chunks.append(data)

    def write_array_header(self, n: int) -> None:
        if n <= 15:
            self._chunks.append(bytes((0x90 | n,)))
        elif n <= 0xFFFF:
            self._chunks.append(b"\xdc" + _U16.pack(n))
        elif n <= 0xFFFFFFFF:
            self._chunks.append(b"\xdd" + _U32.pack(n))
        else:
            raise MsgPackError(f"Array too large for MsgPack: {n}")

    def write_map_header(self, n: int) -> None:
        if n <= 15:
            self._chunks.append(bytes((0x80 | n,)))
        elif n <= 0xFFFF:
            self._chunks.append(b"\xde" + _U16.pack(n))
        elif n <= 0xFFFFFFFF:
            self._chunks.append(b"\xdf" + _U32.pack(n))
        else:
            raise MsgPackError(f"Map too large for MsgPack: {n}")

    def write(self, val: Any) -> None:
        if val is None:
            self.write_nil()
        elif isinstance(val, bool):
            self.write_bool(val)
        elif isinstance(val, int):
            self.write_int(val)
        elif isinstance(val, float):
            self.write_float32(val)
        elif isinstance(val, str):
            self.write_str(val)
        elif isinstance(val, (bytes, bytearray, memoryview)):
            self.write_bin(val)
        elif isinstance(val, (list, tuple)):
            self.write_array_header(len(val))
            for item in val:
                self.write(item)
        elif isinstance(val, dict):
            self.write_map_header(len(val))
            for k, v in val.items():
                self.write(k)
                self.write(v)
        else:
            raise MsgPackError(f"Unsupported type for MsgPack serialization: {type(val).__name__}")
