# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Optional ctypes loader for the C++17 fastfmt accelerator library.

Loads from io_scene_collada/_native/ if present and COLLADA_NATIVE != "0".
Falls back silently to pure Python / NumPy if unavailable.
"""

from __future__ import annotations

import ctypes
import os
import sys

_LIB: ctypes.CDLL | None = None
_LOADED: bool = False


def _lib_filename() -> str:
    if sys.platform == "win32":
        return "fastfmt.dll"
    if sys.platform == "darwin":
        return "libfastfmt.dylib"
    return "libfastfmt.so"


def get_native_lib() -> ctypes.CDLL | None:
    """Return loaded fastfmt CDLL, or None if disabled or unavailable."""
    global _LIB, _LOADED
    if os.environ.get("COLLADA_NATIVE", "1") == "0":
        return None
    if _LOADED:
        return _LIB
    _LOADED = True
    lib_path = os.path.join(os.path.dirname(__file__), "_native", _lib_filename())
    if not os.path.isfile(lib_path):
        return None
    try:
        lib = ctypes.CDLL(lib_path)
        lib.fmt_f32_array.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
        lib.fmt_f32_array.restype = ctypes.c_size_t

        lib.fmt_f32_array_mt.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int]
        lib.fmt_f32_array_mt.restype = ctypes.c_size_t

        lib.fmt_i32_array.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
        lib.fmt_i32_array.restype = ctypes.c_size_t

        lib.fmt_i32_array_mt.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int]
        lib.fmt_i32_array_mt.restype = ctypes.c_size_t

        lib.parse_f32.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
        lib.parse_f32.restype = ctypes.c_size_t

        lib.parse_i32.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
        lib.parse_i32.restype = ctypes.c_size_t

        _LIB = lib
    except OSError:
        _LIB = None
    return _LIB


def native_fmt_f32_array(arr) -> str | None:
    lib = get_native_lib()
    if lib is None:
        return None
    import numpy as np

    a = np.ascontiguousarray(arr, dtype=np.float32).ravel()
    n = a.size
    if n == 0:
        return ""
    buf = ctypes.create_string_buffer(n * 16 + 1)
    nthreads = min(os.cpu_count() or 1, 8)
    written = lib.fmt_f32_array_mt(a.ctypes.data, n, buf, nthreads)
    return buf.raw[:written].decode("ascii")


def native_fmt_i32_array(arr) -> str | None:
    lib = get_native_lib()
    if lib is None:
        return None
    import numpy as np

    a = np.ascontiguousarray(arr, dtype=np.int32).ravel()
    n = a.size
    if n == 0:
        return ""
    buf = ctypes.create_string_buffer(n * 12 + 1)
    nthreads = min(os.cpu_count() or 1, 8)
    written = lib.fmt_i32_array_mt(a.ctypes.data, n, buf, nthreads)
    return buf.raw[:written].decode("ascii")


def native_parse_f32(text: str):
    lib = get_native_lib()
    if lib is None:
        return None
    import numpy as np

    raw = text.encode("ascii", errors="replace")
    if not raw:
        return np.empty(0, dtype=np.float32)
    cap = len(raw) // 2 + 2
    out = np.empty(cap, dtype=np.float32)
    ok = ctypes.c_int(0)
    n = lib.parse_f32(raw, len(raw), out.ctypes.data, cap, ctypes.byref(ok))
    if ok.value == 0:
        return None
    return out[:n].copy()


def native_parse_i32(text: str):
    lib = get_native_lib()
    if lib is None:
        return None
    import numpy as np

    raw = text.encode("ascii", errors="replace")
    if not raw:
        return np.empty(0, dtype=np.int32)
    cap = len(raw) // 2 + 2
    out = np.empty(cap, dtype=np.int32)
    ok = ctypes.c_int(0)
    n = lib.parse_i32(raw, len(raw), out.ctypes.data, cap, ctypes.byref(ok))
    if ok.value == 0:
        return None
    return out[:n].copy()
