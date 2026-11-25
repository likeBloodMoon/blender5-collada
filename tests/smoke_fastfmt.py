"""Fast cross-platform smoke test for native/fastfmt (Windows, macOS, Linux) (#4).

Loads the built native library via io_scene_collada/_native_loader.py and verifies
formatting and parsing parity against pure Python, including edge cases (+1, -0.0,
NaN/inf, 1.#QNAN, empty input, and trailing garbage).
"""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_native_loader():
    path = os.path.join(ROOT, "io_scene_collada", "_native_loader.py")
    spec = importlib.util.spec_from_file_location("_native_loader", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_native_loader"] = mod
    spec.loader.exec_module(mod)
    return mod


def _py_fmt_f32_scalar(v: float) -> str:
    if v == 0.0:
        return "0"
    av = -v if v < 0.0 else v
    if av < 1e-4 or av >= 1e7:
        s = f"{v:.6g}"
        if "e-0" in s:
            s = s.replace("e-0", "e-")
        if "e+0" in s:
            s = s.replace("e+0", "e+")
        return s
    return f"{v:.7g}"


def _py_fmt_f32(arr: np.ndarray) -> str:
    a = np.asarray(arr, dtype=np.float32).ravel()
    if a.size == 0:
        return ""
    if not np.isfinite(a).all():
        a = np.where(np.isfinite(a), a, np.float32(0.0))
    if (a == 0.0).any():
        a = a.copy()
        a[a == 0.0] = 0.0
    return " ".join(_py_fmt_f32_scalar(x) for x in a.tolist())


def main() -> int:
    nl = _load_native_loader()
    lib = nl.get_native_lib()
    if lib is None:
        print("FAIL: native fastfmt library could not be loaded")
        return 1

    # 1. Float32 formatting (normal + edge cases + empty)
    f_cases = [
        np.array([], dtype=np.float32),
        np.array([0.0, -0.0, 1.0, -1.0, 0.1234567, 1e-4, 9.9999e-5, 9.9999e6, 1e7, 3.4e38, 1.4e-45], dtype=np.float32),
        np.array([np.nan, np.inf, -np.inf, 42.5], dtype=np.float32),
    ]
    for arr in f_cases:
        got = nl.native_fmt_f32_array(arr)
        exp = _py_fmt_f32(arr)
        if got != exp:
            print(f"FAIL fmt_f32 mismatch:\n  got={got!r}\n  exp={exp!r}")
            return 1

    # 2. Int32 formatting
    i_cases = [
        np.array([], dtype=np.int32),
        np.array([0, 1, -1, 42, -2147483648, 2147483647], dtype=np.int32),
    ]
    for arr in i_cases:
        got = nl.native_fmt_i32_array(arr)
        exp = " ".join(str(int(x)) for x in arr.tolist())
        if got != exp:
            print(f"FAIL fmt_i32 mismatch:\n  got={got!r}\n  exp={exp!r}")
            return 1

    # 3. Float32 parsing (valid + edge cases)
    p_valid = nl.native_parse_f32("+1 -2.5 0.0 1e-5 3.14159")
    assert p_valid is not None and np.allclose(p_valid, [1.0, -2.5, 0.0, 1e-5, 3.14159], rtol=1e-6), p_valid

    p_empty = nl.native_parse_f32("")
    assert p_empty is not None and p_empty.size == 0, p_empty

    for bad in ("1.0 1.#QNAN 2.0", "1.0 2.0x", "1.0 xyz 3.0"):
        got_bad = nl.native_parse_f32(bad)
        if got_bad is not None:
            print(f"FAIL parse_f32({bad!r}) expected None fallback, got {got_bad!r}")
            return 1

    # 4. Int32 parsing (valid + edge cases)
    pi_valid = nl.native_parse_i32("+1 0 -42 123456")
    assert pi_valid is not None and pi_valid.tolist() == [1, 0, -42, 123456], pi_valid

    pi_empty = nl.native_parse_i32("")
    assert pi_empty is not None and pi_empty.size == 0, pi_empty

    for bad in ("1 2x 3", "1 1.5 3", "abc"):
        got_bad = nl.native_parse_i32(bad)
        if got_bad is not None:
            print(f"FAIL parse_i32({bad!r}) expected None fallback, got {got_bad!r}")
            return 1

    print(f"PASS smoke_fastfmt on {sys.platform} ({os.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
